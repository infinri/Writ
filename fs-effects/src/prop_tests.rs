//! CO-PROP-01 for the `fs.write` adapter, single worker (M5a-10; D-040; D-045 M5-D8;
//! [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-7 revision 3).
//!
//! A seeded generator drives sequences of steps against one root through the real broker,
//! the real adapter, the real `writ-fseffects-1` journal and the real filesystem:
//!
//! - **sequential operations** (`create_only` and `replace_if` over three targets, sources
//!   with equal and different bytes), each optionally decorated with one injected journal
//!   fault (`test-faults`: every adapter transaction, begin, body, commit lost, commit done;
//!   `SQLITE_BUSY` or, rarely, `SQLITE_IOERR`), one injected filesystem fault (create, write,
//!   `fsync`s, `statx`, rename, observation, cleanup), a foreign writer acting at a crash
//!   boundary (equal bytes, other bytes, delete, staging swap, hard links), or a simulated
//!   crash at a boundary B0..B12 (the thread unwinds out of `apply`; everything is dropped
//!   and reopened like a process restart; real SIGKILLs: [`sigkill_property`]);
//! - **concurrent groups** of 2 to 4 operations on threads released together, with one
//!   journal fault and one filesystem fault armed for the whole group;
//! - **operator recovery runs** (optionally faulted) and **reopens** (recovery and GC).
//!
//! **Model.** An independent per-key model tracks every operation by its index (mapped to
//! its permit through the journal's idempotency key), never by content: every file is a
//! [`Fid`] (the staged file of op `i`, or the file of a foreign writer), every name (target,
//! staging name, hard link) holds a `Fid`, and the J-7.5 steps and the J-7.6 table are
//! encoded from the contract. After every step the model's prediction of each op's result
//! class, every journal row (state, prior, staged identity, outcome, displaced identity,
//! target identity after), every target row (blocked, taint, last identity), the root's
//! disabled state and every name with its identity and bytes must equal what the real
//! adapter produced. A concurrent group must match the model under **some** serialization
//! (all permutations are tried; one-shot faults are consumed by whichever op reaches them
//! first in that order). At the end of a case, the final state after recovery is checked
//! against the serialization of the committed operations (per target: the last applied op's
//! staged file, unless a foreign write or an unknown outcome intervened), receipts are
//! reconciled and no retry is ever redispatched.
//!
//! **Invariants** checked on the real state after every step, independently of the model:
//! no completed claim without staged-inode evidence; unknown never redispatched (and never
//! revisited: recorded outcomes never change); a target stays blocked while one of its ops
//! is unresolved; garbage collection and cleanup remove only recorded files of applied or
//! failed ops on untainted, unblocked targets (or the live apply's own abandoned file, L-b);
//! a foreign writer's file (equal bytes included) is never completed.
//!
//! **Mutants** ([`crate::Mutant`]): treating content equality as completion, releasing the
//! block at T-staged, collecting tainted targets, skipping the unrecoverable mark on an
//! `fsync` error; each must be rejected.
//!
//! CI profile: `cargo test -p writ-fs-effects --lib prop_tests` (part of the workspace
//! tests). Deeper runs: `WRIT_FSFX_PROP_CASES=<n> WRIT_FSFX_PROP_SEED=<seed>`.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier, Mutex};

use writ_broker::effects::EffectAdapter;
use writ_broker::execute::{Executed, Execution};
use writ_broker::store::TxStage;
use writ_hostsys::fsfx::{FxError, EACCES, EIO, ENOSPC};
use writ_ledger_sqlite::{FaultCode, FaultStage, JournalFault, OpState};

use crate::ident::Identity;
use crate::test_support::*;
use crate::{Boundary, FsWriteAdapter, Mutant, Step as Fx, MUTANTS};

const TARGETS: [&str; 3] = ["sub/p0.txt", "sub/p1.txt", "p2.txt"];
const SOURCES: [&str; 3] = ["a.txt", "b.txt", "same.txt"];
const FOREIGN: &[u8] = b"foreign writer bytes\n";
const INITIAL: &[u8] = b"initial content placed before the adapter\n";
/// The step index of the initial file's writer.
const INIT_STEP: usize = usize::MAX;

// ---------------------------------------------------------------------------------------
// Seeded generator.

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // splitmix64
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    fn pct(&mut self, p: usize) -> bool {
        self.below(100) < p
    }

    fn pick<T: Copy>(&mut self, xs: &[T]) -> T {
        xs[self.below(xs.len())]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    Create,
    Replace,
}

#[derive(Clone, Debug)]
struct OpSpec {
    target: usize,
    mode: Mode,
    src: usize,
    /// `replace_if`: the expected prior bytes.
    expected: Option<Vec<u8>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum JStage {
    Begin,
    Body,
    CommitLost,
    CommitDone,
}

#[derive(Clone, Copy, Debug)]
struct JFault {
    tx: &'static str,
    stage: JStage,
    io: bool,
}

#[derive(Clone, Copy, Debug)]
struct FFault {
    step: Fx,
    errno: i32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Act {
    /// Replace the target with a new file holding the op's own bytes (equal content).
    SameBytes,
    /// Replace the target with a new file holding other bytes.
    OtherBytes,
    /// Delete the target.
    Delete,
    /// Replace the op's staging file with a new file holding the same bytes.
    SwapStaging,
    /// Hard-link the target to `sub/hl-<step>`.
    LinkTarget,
    /// Hard-link the op's staging file to `sub/hl-<step>`.
    LinkStaging,
}

#[derive(Clone, Debug)]
enum Deco {
    None,
    J(JFault),
    F(FFault),
    Foreign(Boundary, Act),
    Crash(Boundary),
}

#[derive(Clone, Debug)]
enum StepKind {
    /// An op, its decoration, and an extra fault combined with a foreign writer or a crash.
    Seq(OpSpec, Deco, Option<JFault>, Option<FFault>),
    Group(Vec<OpSpec>, Option<JFault>, Option<FFault>),
    Recover(Option<JFault>, Option<FFault>),
    Reopen,
}

/// Generation weights.
#[derive(Clone, Copy, Debug)]
struct Profile {
    cases: usize,
    steps: usize,
    /// No groups and no faulted recovery runs (the SIGKILL property re-simulates a prefix).
    sequential_only: bool,
}

const ALL_TX: [&str; 8] = [
    "fs_intent",
    "fs_staged",
    "fs_outcome",
    "fs_resolve",
    "fs_abandon",
    "fs_drift",
    "fs_recover",
    "fs_recover_scan",
];

fn gen_jfault(rng: &mut Rng, txs: &[&'static str]) -> JFault {
    JFault {
        tx: rng.pick(txs),
        stage: rng.pick(&[
            JStage::Begin,
            JStage::Body,
            JStage::CommitLost,
            JStage::CommitDone,
        ]),
        io: rng.pct(6),
    }
}

fn gen_ffault(rng: &mut Rng, recovery: bool) -> FFault {
    let step = if rng.pct(8) {
        rng.pick(&[Fx::FsyncFile, Fx::FsyncStaging, Fx::FsyncParent])
    } else if recovery {
        Fx::ObserveTarget
    } else {
        rng.pick(&[
            Fx::Create,
            Fx::Write,
            Fx::StatxStaged,
            Fx::Rename,
            Fx::ObserveTarget,
            Fx::Cleanup,
        ])
    };
    FFault {
        step,
        errno: rng.pick(&[ENOSPC, EACCES, EIO]),
    }
}

const BOUNDARIES: [Boundary; 13] = [
    Boundary::B0,
    Boundary::B1,
    Boundary::B2,
    Boundary::B3,
    Boundary::B4,
    Boundary::B5,
    Boundary::B6,
    Boundary::B7,
    Boundary::B8,
    Boundary::B9,
    Boundary::B10,
    Boundary::B11,
    Boundary::B12,
];

fn gen_op(rng: &mut Rng, m: &Model, target: usize) -> OpSpec {
    let cur = m
        .names
        .get(&Name::Target(target))
        .map(|f| m.content[f].clone());
    let replace = match &cur {
        Some(_) => rng.pct(75),
        None => rng.pct(12),
    };
    let src = rng.below(SOURCES.len());
    if !replace {
        return OpSpec {
            target,
            mode: Mode::Create,
            src,
            expected: None,
        };
    }
    let expected = match cur {
        Some(c) if rng.pct(85) => c,
        _ => match rng.below(3) {
            0 => FOREIGN.to_vec(),
            1 => INITIAL.to_vec(),
            _ => source_bytes(rng.below(SOURCES.len())),
        },
    };
    OpSpec {
        target,
        mode: Mode::Replace,
        src,
        expected: Some(expected),
    }
}

fn gen_step(rng: &mut Rng, m: &Model, p: &Profile) -> StepKind {
    let roll = rng.below(100);
    if roll < 52 || (p.sequential_only && roll < 82) {
        let target = rng.below(TARGETS.len());
        let op = gen_op(rng, m, target);
        let deco = match rng.below(100) {
            0..=29 => Deco::None,
            30..=49 => Deco::J(gen_jfault(rng, &ALL_TX[..6])),
            50..=67 => Deco::F(gen_ffault(rng, false)),
            68..=87 => Deco::Foreign(
                rng.pick(&[
                    Boundary::B0,
                    Boundary::B1,
                    Boundary::B3,
                    Boundary::B5,
                    Boundary::B6,
                    Boundary::B7,
                    Boundary::B8,
                    // After the rename, the M5-D8 case (equal bytes are not evidence), twice.
                    Boundary::B8,
                    Boundary::B9,
                    Boundary::B10,
                    Boundary::B11,
                ]),
                rng.pick(&[
                    Act::SameBytes,
                    Act::SameBytes,
                    Act::OtherBytes,
                    Act::Delete,
                    Act::SwapStaging,
                    Act::LinkTarget,
                    Act::LinkStaging,
                ]),
            ),
            _ => Deco::Crash(rng.pick(&BOUNDARIES)),
        };
        let combined = matches!(deco, Deco::Foreign(..) | Deco::Crash(_));
        let (jf, ff) = match rng.below(100) {
            0..=19 if combined => (Some(gen_jfault(rng, &ALL_TX[..6])), None),
            20..=34 if combined => (None, Some(gen_ffault(rng, false))),
            _ => (None, None),
        };
        StepKind::Seq(op, deco, jf, ff)
    } else if roll < 82 {
        let n = 2 + rng.below(3);
        let hot = rng.below(TARGETS.len());
        let ops = (0..n)
            .map(|_| {
                let t = if rng.pct(60) {
                    hot
                } else {
                    rng.below(TARGETS.len())
                };
                gen_op(rng, m, t)
            })
            .collect();
        let jf = rng.pct(55).then(|| gen_jfault(rng, &ALL_TX[..6]));
        let ff = rng.pct(35).then(|| gen_ffault(rng, false));
        StepKind::Group(ops, jf, ff)
    } else if roll < 90 {
        if p.sequential_only {
            return StepKind::Recover(None, None);
        }
        let jf = rng
            .pct(40)
            .then(|| gen_jfault(rng, &["fs_recover", "fs_recover_scan"]));
        let ff = rng.pct(40).then(|| gen_ffault(rng, true));
        StepKind::Recover(jf, ff)
    } else {
        StepKind::Reopen
    }
}

fn source_bytes(src: usize) -> Vec<u8> {
    match src {
        1 => b"bravo content, longer than alpha\n".to_vec(),
        _ => b"alpha content\n".to_vec(),
    }
}

// ---------------------------------------------------------------------------------------
// The model.

/// A file, by identity: the staged file of op `i`, or a foreign writer's file of step `k`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Fid {
    Op(usize),
    Foreign(usize),
    /// A real file the harness cannot attribute (always a failure).
    Unknown(u64),
}

/// A name under the root.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Name {
    Target(usize),
    Staging(usize),
    Link(usize),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum St {
    Intent,
    Abandoned,
    Staged,
    Resolved,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Out {
    result: String,
    reason: String,
    evidence: String,
    displaced: Option<Fid>,
    /// `target_identity_after`: `None` not recorded, `Some(None)` absent.
    after: Option<Option<Fid>>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Rec {
    state: St,
    /// `None`: absent.
    prior: Option<Fid>,
    staged: Option<Fid>,
    out: Option<Out>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct TRow {
    blocked_by: Option<usize>,
    tainted: bool,
    taint_reason: Option<String>,
    /// `None`: no last identity; `Some(None)`: absent.
    last: Option<Option<Fid>>,
}

/// The observable state (real or predicted).
#[derive(Clone, Debug, PartialEq, Eq)]
struct Snap {
    recs: BTreeMap<usize, Rec>,
    targets: BTreeMap<usize, TRow>,
    names: BTreeMap<Name, (Fid, Vec<u8>)>,
    disabled: bool,
}

/// The result class of one execution.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Res {
    Applied,
    Failed,
    /// Outer `Begin` `ROOT_UNRECOVERABLE` (nothing consumed).
    ErrUnrec,
    /// Any other adapter failure.
    ErrOther,
    /// The thread unwound at a crash boundary.
    Crashed,
    /// Anything else (never predicted).
    Unexpected(String),
}

#[derive(Clone, Debug)]
struct MOp {
    spec: OpSpec,
    op_id: Option<String>,
}

#[derive(Clone, Debug)]
struct Model {
    ops: Vec<MOp>,
    recs: BTreeMap<usize, Rec>,
    names: BTreeMap<Name, Fid>,
    content: BTreeMap<Fid, Vec<u8>>,
    targets: BTreeMap<usize, TRow>,
    disabled: bool,
    jf: Option<JFault>,
    ff: Option<FFault>,
    foreign: Option<(Boundary, Act, usize)>,
    crash: Option<Boundary>,
}

type RecoverOut = Result<Vec<(usize, &'static str)>, Res>;

/// (operation evidence, result, target after, removal) of a J-7.6 row.
fn plan(row: &str) -> Option<(&'static str, &'static str, &'static str, &'static str)> {
    Some(match row {
        "R1a" => ("not_completed", "failed", "prior", ""),
        "R1b" => ("not_completed", "failed", "blocked", ""),
        "R1c" => ("not_completed", "failed", "prior", "staged"),
        "R3" => ("completed", "applied", "staged", ""),
        "R3x" => ("completed", "applied", "staged", "displaced"),
        "R5" => ("not_completed", "failed", "prior", "staged"),
        "R2" | "R4" | "R6" | "R7" | "R8" | "R3y" | "R-other" => ("unknown", "unknown", "taint", ""),
        _ => return None,
    })
}

impl Model {
    fn new() -> Model {
        let mut m = Model {
            ops: Vec::new(),
            recs: BTreeMap::new(),
            names: BTreeMap::new(),
            content: BTreeMap::new(),
            targets: BTreeMap::new(),
            disabled: false,
            jf: None,
            ff: None,
            foreign: None,
            crash: None,
        };
        m.names.insert(Name::Target(1), Fid::Foreign(INIT_STEP));
        m.content.insert(Fid::Foreign(INIT_STEP), INITIAL.to_vec());
        m
    }

    fn nlink(&self, f: Fid) -> usize {
        self.names.values().filter(|x| **x == f).count()
    }

    fn mark(&mut self) {
        self.disabled = true;
    }

    fn take_ff(&mut self, step: Fx) -> bool {
        if self.ff.is_some_and(|f| f.step == step) {
            self.ff = None;
            true
        } else {
            false
        }
    }

    fn take_jf(&mut self, tx: &str) -> Option<JFault> {
        match self.jf {
            Some(f) if f.tx == tx => self.jf.take(),
            _ => None,
        }
    }

    /// A boundary: run a pending foreign action; `true` when the op crashes here.
    fn at(&mut self, b: Boundary, i: usize) -> bool {
        if let Some((at, act, step)) = self.foreign {
            if at == b {
                self.foreign = None;
                self.act(act, step, i);
            }
        }
        self.crash == Some(b)
    }

    fn act(&mut self, act: Act, step: usize, i: usize) {
        let spec = self.ops[i].spec.clone();
        let t = Name::Target(spec.target);
        let s = Name::Staging(i);
        let f = Fid::Foreign(step);
        match act {
            Act::SameBytes | Act::OtherBytes => {
                let bytes = if act == Act::SameBytes {
                    source_bytes(spec.src)
                } else {
                    FOREIGN.to_vec()
                };
                self.names.insert(t, f);
                self.content.insert(f, bytes);
            }
            Act::Delete => {
                self.names.remove(&t);
            }
            Act::SwapStaging => {
                if let Some(old) = self.names.get(&s).copied() {
                    let bytes = self.content[&old].clone();
                    self.names.insert(s, f);
                    self.content.insert(f, bytes);
                }
            }
            Act::LinkTarget | Act::LinkStaging => {
                let from = if act == Act::LinkTarget { t } else { s };
                if let Some(x) = self.names.get(&from).copied() {
                    self.names.insert(Name::Link(step), x);
                }
            }
        }
    }

    fn target_row(&mut self, t: usize) -> &mut TRow {
        self.targets.entry(t).or_default()
    }

    /// One journal transaction's injected fault: `Err` when the transaction fails before or
    /// instead of its commit (nothing durable); `Ok(true)` when it committed but reports an
    /// unknown commit outcome.
    fn jfault_end(&mut self, jf: Option<JFault>, commits: bool) -> Result<bool, ()> {
        match jf {
            None => Ok(false),
            Some(f) => match f.stage {
                JStage::Begin | JStage::Body | JStage::CommitLost => {
                    if f.io {
                        self.mark();
                    }
                    Err(())
                }
                JStage::CommitDone => {
                    if commits && f.io {
                        self.mark();
                    }
                    Ok(commits)
                }
            },
        }
    }

    fn begin_fails(&mut self, jf: Option<JFault>) -> bool {
        if let Some(f) = jf {
            if f.stage == JStage::Begin {
                if f.io {
                    self.mark();
                }
                return true;
            }
        }
        false
    }

    fn content_of(&self, n: Name) -> Option<&Vec<u8>> {
        self.names.get(&n).map(|f| &self.content[f])
    }

    /// The J-5 normal path of op `i` (J-7.5).
    fn apply(&mut self, i: usize) -> Res {
        if self.disabled {
            return Res::ErrUnrec;
        }
        let spec = self.ops[i].spec.clone();
        let jf = self.take_jf("fs_intent");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        // T-intent body.
        if self.take_ff(Fx::ObserveTarget) {
            // Rolled back; a CommitDone fault on a rollback end is silent.
            if let Some(f) = jf {
                if f.stage != JStage::CommitDone && f.io {
                    self.mark();
                }
            }
            return Res::ErrOther;
        }
        let t = self.names.get(&Name::Target(spec.target)).copied();
        let prior = match spec.mode {
            Mode::Create => t.is_none().then_some(None),
            Mode::Replace => match t {
                Some(f)
                    if self.nlink(f) == 1 && Some(&self.content[&f]) == spec.expected.as_ref() =>
                {
                    Some(Some(f))
                }
                _ => None,
            },
        };
        let row = self.targets.get(&spec.target).cloned();
        let failed = match prior {
            None => Some(("TARGET_PRECONDITION_FAILED", false)),
            Some(p) => match &row {
                Some(r) if r.blocked_by.is_some() => Some(("TARGET_UNRESOLVED", false)),
                Some(r) if r.tainted => Some(("TARGET_TAINTED", false)),
                Some(r) if r.last.is_some() && r.last != Some(p) => Some(("TARGET_TAINTED", true)),
                _ => None,
            },
        };
        if failed.is_none() && self.at(Boundary::B0, i) {
            return Res::Crashed;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        if let Some((reason, r11)) = failed {
            if r11 {
                let r = self.target_row(spec.target);
                r.tainted = true;
                r.taint_reason = Some("R11".into());
            }
            self.recs.insert(
                i,
                Rec {
                    state: St::Resolved,
                    prior: match (prior, reason) {
                        (Some(p), "TARGET_UNRESOLVED" | "TARGET_TAINTED") => p,
                        _ => None,
                    },
                    staged: None,
                    out: Some(Out {
                        result: "failed".into(),
                        reason: reason.into(),
                        evidence: "not_completed".into(),
                        displaced: None,
                        after: None,
                    }),
                },
            );
            return if unknown { Res::ErrOther } else { Res::Failed };
        }
        let prior = prior.unwrap();
        self.recs.insert(
            i,
            Rec {
                state: St::Intent,
                prior,
                staged: None,
                out: None,
            },
        );
        let r = self.target_row(spec.target);
        r.blocked_by = Some(i);
        r.tainted = false;
        r.taint_reason = None;
        if unknown {
            return Res::ErrOther;
        }
        if self.at(Boundary::B1, i) {
            return Res::Crashed;
        }
        let res = self.after_intent(i, &spec, prior);
        if res == Res::Crashed || self.at(Boundary::B12, i) {
            return Res::Crashed;
        }
        res
    }

    fn after_intent(&mut self, i: usize, spec: &OpSpec, prior: Option<Fid>) -> Res {
        let s = Name::Staging(i);
        let t = Name::Target(spec.target);
        let own = Fid::Op(i);
        let bytes = source_bytes(spec.src);
        if self.take_ff(Fx::Create) {
            return self.resolve_failed(i, "STAGE_FAILED");
        }
        self.names.insert(s, own);
        self.content.insert(own, Vec::new());
        if self.take_ff(Fx::Write) {
            return self.abandon(i);
        }
        self.content.insert(own, bytes[..bytes.len() / 2].to_vec());
        if self.at(Boundary::B2, i) {
            return Res::Crashed;
        }
        self.content.insert(own, bytes.clone());
        if self.at(Boundary::B3, i) {
            return Res::Crashed;
        }
        if self.take_ff(Fx::FsyncFile) {
            self.mark();
            return Res::ErrOther;
        }
        let staged_ok = self.content_of(s) == Some(&bytes);
        if self.take_ff(Fx::StatxStaged) {
            return Res::ErrOther;
        }
        if !staged_ok || self.nlink(own) != 1 {
            return self.abandon(i);
        }
        if self.take_ff(Fx::FsyncStaging) {
            self.mark();
            return Res::ErrOther;
        }
        if self.at(Boundary::B4, i) {
            return Res::Crashed;
        }
        let jf = self.take_jf("fs_staged");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        let rec = self.recs.get_mut(&i).unwrap();
        rec.state = St::Staged;
        rec.staged = Some(own);
        if unknown {
            return Res::ErrOther;
        }
        if self.at(Boundary::B5, i) || self.at(Boundary::B6, i) {
            return Res::Crashed;
        }
        // Recheck.
        if self.take_ff(Fx::ObserveTarget) {
            return self.reobserve(i);
        }
        if self.names.get(&s) != Some(&own)
            || self.nlink(own) != 1
            || self.names.get(&t).copied() != prior
        {
            return self.reobserve(i);
        }
        if self.at(Boundary::B7, i) {
            return Res::Crashed;
        }
        if self.take_ff(Fx::Rename) {
            return self.reobserve(i);
        }
        match spec.mode {
            Mode::Create => {
                if self.names.contains_key(&t) {
                    return self.drift(i);
                }
                match self.names.remove(&s) {
                    Some(f) => {
                        self.names.insert(t, f);
                    }
                    None => return self.reobserve(i),
                }
            }
            Mode::Replace => {
                let (Some(a), Some(b)) = (self.names.get(&s).copied(), self.names.get(&t).copied())
                else {
                    return self.reobserve(i);
                };
                self.names.insert(s, b);
                self.names.insert(t, a);
            }
        }
        if self.at(Boundary::B8, i) {
            return Res::Crashed;
        }
        if self.take_ff(Fx::FsyncParent) || self.take_ff(Fx::FsyncStaging) {
            self.mark();
            return Res::ErrOther;
        }
        if self.at(Boundary::B9, i) {
            return Res::Crashed;
        }
        // Verify.
        if self.take_ff(Fx::ObserveTarget) {
            return self.reobserve(i);
        }
        let target_ok = self.names.get(&t) == Some(&own) && self.nlink(own) == 1;
        let staging_ok = match prior {
            None => !self.names.contains_key(&s),
            Some(p) => {
                self.names.get(&s) == Some(&p)
                    && self.nlink(p) == 1
                    && Some(&self.content[&p]) == spec.expected.as_ref()
            }
        };
        if !(target_ok && staging_ok) {
            return self.reobserve(i);
        }
        if self.at(Boundary::B10, i) {
            return Res::Crashed;
        }
        let jf = self.take_jf("fs_outcome");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        let rec = self.recs.get_mut(&i).unwrap();
        rec.state = St::Resolved;
        rec.out = Some(Out {
            result: "applied".into(),
            reason: "applied".into(),
            evidence: "completed".into(),
            displaced: prior,
            after: Some(Some(own)),
        });
        let r = self.target_row(spec.target);
        *r = TRow {
            blocked_by: None,
            tainted: false,
            taint_reason: None,
            last: Some(Some(own)),
        };
        if unknown {
            return Res::ErrOther;
        }
        if self.at(Boundary::B11, i) {
            return Res::Crashed;
        }
        if let Some(p) = prior {
            if !self.take_ff(Fx::Cleanup) && self.names.get(&s) == Some(&p) {
                self.names.remove(&s);
                if self.take_ff(Fx::FsyncStaging) {
                    self.mark();
                }
            }
        }
        Res::Applied
    }

    fn resolve_failed(&mut self, i: usize, reason: &str) -> Res {
        let jf = self.take_jf("fs_resolve");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        let target = self.ops[i].spec.target;
        let rec = self.recs.get_mut(&i).unwrap();
        rec.state = St::Resolved;
        rec.out = Some(Out {
            result: "failed".into(),
            reason: reason.into(),
            evidence: "not_completed".into(),
            displaced: None,
            after: None,
        });
        let prior = rec.prior;
        *self.target_row(target) = TRow {
            blocked_by: None,
            tainted: false,
            taint_reason: None,
            last: Some(prior),
        };
        if unknown {
            Res::ErrOther
        } else {
            Res::Failed
        }
    }

    fn abandon(&mut self, i: usize) -> Res {
        let jf = self.take_jf("fs_abandon");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        let rec = self.recs.get_mut(&i).unwrap();
        rec.state = St::Abandoned;
        rec.staged = Some(Fid::Op(i));
        if unknown {
            return Res::ErrOther;
        }
        if self.names.get(&Name::Staging(i)) == Some(&Fid::Op(i)) {
            self.names.remove(&Name::Staging(i));
        }
        if self.take_ff(Fx::FsyncStaging) {
            self.mark();
            return Res::ErrOther;
        }
        self.resolve_failed(i, "STAGE_FAILED")
    }

    fn drift(&mut self, i: usize) -> Res {
        let jf = self.take_jf("fs_drift");
        if self.begin_fails(jf) {
            return Res::ErrOther;
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Res::ErrOther;
        };
        let target = self.ops[i].spec.target;
        let rec = self.recs.get_mut(&i).unwrap();
        rec.state = St::Resolved;
        rec.out = Some(Out {
            result: "failed".into(),
            reason: "drift".into(),
            evidence: "not_completed".into(),
            displaced: None,
            after: None,
        });
        let r = self.target_row(target);
        r.blocked_by = None;
        r.tainted = true;
        r.taint_reason = Some("drift".into());
        if unknown {
            Res::ErrOther
        } else {
            Res::Failed
        }
    }

    fn reobserve(&mut self, i: usize) -> Res {
        let row = match self.recover(Some(i)) {
            Err(e) => return e,
            Ok(rows) => rows.first().map_or("R9", |r| r.1),
        };
        match plan(row).map(|p| p.1) {
            Some("applied") => Res::Applied,
            Some("failed") => Res::Failed,
            _ => Res::ErrOther,
        }
    }

    /// Recovery (J-7.6) of every unresolved op in `op_id` order, or of `only`.
    fn recover(&mut self, only: Option<usize>) -> RecoverOut {
        if self.disabled {
            return Err(Res::ErrUnrec);
        }
        let jf = self.take_jf("fs_recover_scan");
        if self.begin_fails(jf) || self.jfault_end(jf, false).is_err() {
            return Err(Res::ErrOther);
        }
        let mut ops: Vec<(String, usize)> = self
            .recs
            .iter()
            .filter(|(_, r)| r.state != St::Resolved)
            .map(|(i, _)| (self.ops[*i].op_id.clone().unwrap_or_default(), *i))
            .collect();
        ops.sort();
        let mut out = Vec::new();
        for (_, j) in ops {
            if only.is_some_and(|o| o != j) {
                continue;
            }
            let row = self.recover_op(j)?;
            out.push((j, row));
            if self.disabled {
                break;
            }
        }
        Ok(out)
    }

    /// The J-7.6 decision table, first match wins (N1).
    fn decide(&self, j: usize, t: Option<Fid>, s: Option<Fid>) -> &'static str {
        let rec = &self.recs[&j];
        let spec = &self.ops[j].spec;
        let replace = spec.mode == Mode::Replace;
        let content_ok = |f: Fid| self.content[&f] == source_bytes(spec.src);
        match rec.state {
            St::Intent => {
                if t != rec.prior {
                    "R2"
                } else if s.is_none() {
                    "R1a"
                } else {
                    "R1b"
                }
            }
            St::Abandoned => match rec.staged {
                Some(id) if t == rec.prior && (s.is_none() || s == Some(id)) => "R1c",
                _ => "R-other",
            },
            St::Staged => {
                let Some(id) = rec.staged else {
                    return "R-other";
                };
                let t_staged = t == Some(id);
                let s_staged = s == Some(id);
                let s_prior = rec.prior.is_some() && s == rec.prior;
                let s_other = s.is_some() && !s_staged && !(replace && s_prior);
                let nl = |f: Option<Fid>| f.map_or(0, |f| self.nlink(f));
                if s_other
                    || (t_staged && nl(t) > 1)
                    || (s_staged && nl(s) > 1)
                    || (t_staged && s_staged)
                {
                    return "R8";
                }
                if t_staged && !content_ok(id) {
                    return "R4";
                }
                let s_expected =
                    s.is_some_and(|f| Some(&self.content[&f]) == spec.expected.as_ref());
                if replace && t_staged && (s.is_none() || !s_prior || !s_expected || nl(s) > 1) {
                    return "R3y";
                }
                if s_staged && t != rec.prior && !t_staged {
                    return "R6";
                }
                if !replace && t_staged && nl(t) == 1 && s.is_none() {
                    return "R3";
                }
                if replace && t_staged && nl(t) == 1 && s_prior && nl(s) == 1 && s_expected {
                    return "R3x";
                }
                if s_staged && nl(s) == 1 && t == rec.prior {
                    return "R5";
                }
                if !replace && s.is_none() && !t_staged {
                    return "R7";
                }
                "R-other"
            }
            St::Resolved => "R-other",
        }
    }

    fn recover_op(&mut self, j: usize) -> Result<&'static str, Res> {
        let target = self.ops[j].spec.target;
        let tn = Name::Target(target);
        let sn = Name::Staging(j);
        if self.take_ff(Fx::ObserveTarget) {
            return Ok("R9");
        }
        if self.take_ff(Fx::FsyncParent) {
            self.mark();
            return Ok("R9");
        }
        if self.names.contains_key(&tn) && self.take_ff(Fx::FsyncFile) {
            self.mark();
            return Ok("R9");
        }
        if self.take_ff(Fx::FsyncStaging) {
            self.mark();
            return Ok("R9");
        }
        let t = self.names.get(&tn).copied();
        let s = self.names.get(&sn).copied();
        let row = self.decide(j, t, s);
        let (evidence, result, after, remove) = plan(row).unwrap();
        let jf = self.take_jf("fs_recover");
        if self.begin_fails(jf) {
            return Err(Res::ErrOther);
        }
        let Ok(unknown) = self.jfault_end(jf, true) else {
            return Err(Res::ErrOther);
        };
        let rec = self.recs.get_mut(&j).unwrap();
        let (prior, staged) = (rec.prior, rec.staged);
        rec.state = St::Resolved;
        rec.out = Some(Out {
            result: result.into(),
            reason: if row == "R1c" { "STAGE_FAILED" } else { row }.into(),
            evidence: evidence.into(),
            displaced: if row == "R3x" { prior } else { None },
            after: Some(t),
        });
        let r = self.target_row(target);
        match after {
            "prior" => {
                r.blocked_by = None;
                r.last = Some(prior);
            }
            "staged" => {
                r.blocked_by = None;
                r.last = Some(staged);
            }
            "blocked" => r.blocked_by = Some(j),
            _ => {
                r.blocked_by = None;
                r.tainted = true;
                if r.taint_reason.is_none() {
                    r.taint_reason = Some(row.into());
                }
            }
        }
        if unknown {
            return Err(Res::ErrOther);
        }
        let victim = match remove {
            "staged" => staged,
            "displaced" => prior,
            _ => None,
        };
        if victim.is_some() && s == victim {
            self.names.remove(&sn);
            if self.take_ff(Fx::FsyncStaging) {
                self.mark();
            }
        }
        Ok(row)
    }

    /// Garbage collection at open (J-7.5 cleanup rule).
    fn gc(&mut self) {
        let names: Vec<(Name, Fid)> = self
            .names
            .iter()
            .filter(|(n, _)| matches!(n, Name::Staging(_)))
            .map(|(n, f)| (*n, *f))
            .collect();
        for (n, f) in names {
            let Name::Staging(j) = n else { continue };
            let Some(rec) = self.recs.get(&j) else {
                continue;
            };
            let Some(out) = &rec.out else { continue };
            let clear = self
                .targets
                .get(&self.ops[j].spec.target)
                .is_some_and(|r| !r.tainted && r.blocked_by.is_none());
            if clear
                && matches!(out.result.as_str(), "applied" | "failed")
                && self.nlink(f) == 1
                && (rec.staged == Some(f) || out.displaced == Some(f))
            {
                self.names.remove(&n);
            }
        }
    }

    /// Adapter open: refusal code, or recovery then GC.
    fn open(&mut self) -> Result<(), String> {
        if self.disabled {
            return Err(crate::ROOT_UNRECOVERABLE.into());
        }
        if self.recover(None).is_err() {
            return Err("RECOVERY".into());
        }
        self.gc();
        if self.disabled {
            return Err(crate::ROOT_UNRECOVERABLE.into());
        }
        Ok(())
    }

    fn snap(&self) -> Snap {
        Snap {
            recs: self.recs.clone(),
            targets: self.targets.clone(),
            names: self
                .names
                .iter()
                .map(|(n, f)| (*n, (*f, self.content[f].clone())))
                .collect(),
            disabled: self.disabled,
        }
    }
}

// ---------------------------------------------------------------------------------------
// The real side.

type Key = (u64, i64, u32);
type Registry = Arc<Mutex<BTreeMap<Key, Fid>>>;

fn key_of(path: &Path) -> Option<Key> {
    let m = std::fs::symlink_metadata(path).ok()?;
    let b = m
        .created()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    Some((m.ino(), i64::try_from(b.as_secs()).ok()?, b.subsec_nanos()))
}

fn key_of_text(text: &str) -> Option<Key> {
    let id = Identity::parse(text)?;
    Some((id.ino, id.btime_sec, id.btime_nsec))
}

/// Write a new file at `path` (a new inode) and register it as `fid`.
fn foreign_write(path: &Path, bytes: &[u8], reg: &Registry, fid: Fid) {
    let _ = std::fs::remove_file(path);
    std::fs::write(path, bytes).unwrap();
    reg.lock().unwrap().insert(key_of(path).unwrap(), fid);
}

struct CrashMark;

/// The initial file of target 1 (written once; a SIGKILL child registers the parent's).
fn place_initial(env: &Env, reg: &Registry) {
    let path = env.tree(TARGETS[1]);
    match key_of(&path) {
        Some(k) => {
            reg.lock().unwrap().insert(k, Fid::Foreign(INIT_STEP));
        }
        None => foreign_write(&path, INITIAL, reg, Fid::Foreign(INIT_STEP)),
    }
}

/// The real state, read directly from the journal and the root.
fn observe(env: &Env, a: &FsWriteAdapter<'_>, m: &mut Model, reg: &Registry) -> Snap {
    let mut recs = BTreeMap::new();
    let mut targets = BTreeMap::new();
    let mut op_ids: BTreeMap<String, usize> = BTreeMap::new();
    let rows = a
        .journal()
        .read("prop", |tx| {
            let mut out = Vec::new();
            for t in TARGETS {
                for op in tx.ops_for_target(t)? {
                    let outcome = tx.outcome(&op.op_id)?;
                    out.push((op, outcome));
                }
            }
            let mut trows = Vec::new();
            for (i, t) in TARGETS.iter().enumerate() {
                if let Some(r) = tx.target(t)? {
                    trows.push((i, r));
                }
            }
            Ok((out, trows))
        })
        .unwrap();
    let mut reg_now = reg.lock().unwrap().clone();
    for (op, _) in &rows.0 {
        let i: usize = op.idempotency_key[1..].parse().unwrap();
        op_ids.insert(op.op_id.clone(), i);
        m.ops[i].op_id = Some(op.op_id.clone());
        if let Some(k) = op.staged_identity.as_deref().and_then(key_of_text) {
            reg_now.insert(k, Fid::Op(i));
        }
    }
    // Names, with own unrecorded staging files attributed to their op.
    let mut names = BTreeMap::new();
    let staging_of: BTreeMap<String, usize> = op_ids
        .iter()
        .map(|(id, i)| (a.staging_name(id), *i))
        .collect();
    let mut found: Vec<(Name, PathBuf)> = Vec::new();
    for (i, t) in TARGETS.iter().enumerate() {
        found.push((Name::Target(i), env.tree(t)));
    }
    for e in std::fs::read_dir(env.tree("sub")).unwrap() {
        let n = e.unwrap().file_name().to_string_lossy().into_owned();
        if let Some(k) = n.strip_prefix("hl-") {
            found.push((
                Name::Link(k.parse().unwrap()),
                env.tree(&format!("sub/{n}")),
            ));
        }
    }
    for n in staging_files(env) {
        match staging_of.get(&n) {
            Some(i) => {
                let p = env.staging().join(&n);
                if let Some(k) = key_of(&p) {
                    reg_now.entry(k).or_insert(Fid::Op(*i));
                }
                found.push((Name::Staging(*i), p));
            }
            None => found.push((Name::Staging(usize::MAX), env.staging().join(&n))),
        }
    }
    let fid = |k: Option<Key>| -> Fid {
        k.and_then(|k| reg_now.get(&k).copied())
            .unwrap_or(Fid::Unknown(k.map_or(0, |k| k.0)))
    };
    for (n, p) in found {
        if let Ok(bytes) = std::fs::read(&p) {
            names.insert(n, (fid(key_of(&p)), bytes));
        }
    }
    let ident = |text: &str| -> Option<Fid> {
        if text == "absent" {
            None
        } else {
            Some(fid(key_of_text(text)))
        }
    };
    for (op, outcome) in &rows.0 {
        let i = op_ids[&op.op_id];
        recs.insert(
            i,
            Rec {
                state: match op.state {
                    OpState::Intent => St::Intent,
                    OpState::Abandoned => St::Abandoned,
                    OpState::Staged => St::Staged,
                    OpState::Resolved => St::Resolved,
                },
                prior: ident(&op.expected_prior),
                staged: op.staged_identity.as_deref().and_then(ident),
                out: outcome.as_ref().map(|o| Out {
                    result: o.result.clone(),
                    reason: o.reason.clone(),
                    evidence: o.operation_evidence.clone(),
                    displaced: o.displaced_identity.as_deref().and_then(ident),
                    after: o.target_identity_after.as_deref().map(ident),
                }),
            },
        );
    }
    for (i, r) in rows.1 {
        targets.insert(
            i,
            TRow {
                blocked_by: r.blocked_by.map(|b| op_ids[&b]),
                tainted: r.tainted,
                taint_reason: r.taint_reason,
                last: r.last_identity.as_deref().map(ident),
            },
        );
    }
    *reg.lock().unwrap() = reg_now;
    Snap {
        recs,
        targets,
        names,
        disabled: a.is_disabled(),
    }
}

fn diff(want: &Snap, got: &Snap) -> String {
    let mut out = Vec::new();
    let keys: BTreeSet<usize> = want.recs.keys().chain(got.recs.keys()).copied().collect();
    for k in keys {
        if want.recs.get(&k) != got.recs.get(&k) {
            out.push(format!(
                "op {k}: model {:?} real {:?}",
                want.recs.get(&k),
                got.recs.get(&k)
            ));
        }
    }
    for k in 0..TARGETS.len() {
        if want.targets.get(&k) != got.targets.get(&k) {
            out.push(format!(
                "target {k}: model {:?} real {:?}",
                want.targets.get(&k),
                got.targets.get(&k)
            ));
        }
    }
    let names: BTreeSet<Name> = want.names.keys().chain(got.names.keys()).copied().collect();
    for n in names {
        let (w, g) = (
            want.names.get(&n).map(|x| x.0),
            got.names.get(&n).map(|x| x.0),
        );
        if want.names.get(&n) != got.names.get(&n) {
            out.push(format!(
                "name {n:?}: model {w:?} real {g:?} (or bytes differ)"
            ));
        }
    }
    if want.disabled != got.disabled {
        out.push(format!(
            "disabled: model {} real {}",
            want.disabled, got.disabled
        ));
    }
    out.join("\n  ")
}

/// Invariants on the real state, independent of the model (module documentation).
fn invariants(prev: &Snap, now: &Snap, m: &Model) -> Result<(), String> {
    for (i, r) in &now.recs {
        let target = m.ops[*i].spec.target;
        if let Some(o) = &r.out {
            // Recorded outcomes never change (unknown is never revisited).
            if let Some(p) = prev.recs.get(i).and_then(|p| p.out.as_ref()) {
                if p != o {
                    return Err(format!("op {i}: recorded outcome changed {p:?} -> {o:?}"));
                }
            }
            if o.result == "applied" || o.evidence == "completed" {
                // No completed claim without staged-inode evidence; a foreign file is never
                // completed.
                if r.staged != Some(Fid::Op(*i)) || o.after != Some(Some(Fid::Op(*i))) {
                    return Err(format!(
                        "op {i}: completed without its own staged identity: {r:?}"
                    ));
                }
                if o.result != "applied" || o.evidence != "completed" {
                    return Err(format!("op {i}: inconsistent outcome {o:?}"));
                }
            }
        }
        if r.state != St::Resolved {
            // Blocked while unresolved.
            let row = now.targets.get(&target);
            if row.and_then(|t| t.blocked_by) != Some(*i) {
                return Err(format!(
                    "op {i} unresolved but target {target} not blocked by it: {row:?}"
                ));
            }
        }
        if r.out.is_none() && r.state == St::Resolved {
            return Err(format!("op {i}: resolved without an outcome"));
        }
    }
    // Files removed from staging (not moved elsewhere) belong to applied or failed ops of
    // clear targets and carry a recorded identity of that op, or are the live apply's own
    // abandoned file (L-b).
    let alive: BTreeSet<Fid> = now.names.values().map(|x| x.0).collect();
    for (n, (f, _)) in &prev.names {
        let Name::Staging(j) = n else { continue };
        if now.names.contains_key(n) || alive.contains(f) {
            continue;
        }
        let rec = now.recs.get(j);
        let abandoned = rec.is_some_and(|r| {
            r.staged == Some(*f)
                && (r.state == St::Abandoned
                    || r.out.as_ref().is_some_and(|o| o.reason == "STAGE_FAILED"))
        });
        let recorded = rec.is_some_and(|r| {
            r.staged == Some(*f) || r.out.as_ref().is_some_and(|o| o.displaced == Some(*f))
        });
        let resolved = rec
            .and_then(|r| r.out.as_ref())
            .is_some_and(|o| matches!(o.result.as_str(), "applied" | "failed"));
        let clear = now
            .targets
            .get(&m.ops[*j].spec.target)
            .is_some_and(|t| !t.tainted && t.blocked_by.is_none());
        if !(abandoned || (recorded && resolved && clear)) {
            return Err(format!(
                "staging file {f:?} of op {j} removed without a resolved, clear, recorded op: {rec:?}"
            ));
        }
    }
    Ok(())
}

fn class(e: &Execution) -> Res {
    match &e.result {
        Ok(Executed::EffectCompleted) => Res::Applied,
        Ok(Executed::EffectFailed) => Res::Failed,
        Ok(Executed::Replayed(_)) => Res::Unexpected("replayed".into()),
        Err(r) => match e.evidence.failure() {
            Some((name, f)) if format!("{name:?}") == "Adapter" => {
                if f.stage == TxStage::Begin && f.error.code() == crate::ROOT_UNRECOVERABLE {
                    Res::ErrUnrec
                } else {
                    Res::ErrOther
                }
            }
            Some((name, f)) => Res::Unexpected(format!("{name:?} {}", f.error.code())),
            None => Res::Unexpected(format!("refused {}", r.code())),
        },
    }
}

fn arm(a: &FsWriteAdapter<'_>, jf: Option<JFault>, ff: Option<FFault>) {
    clear(a);
    if let Some(f) = jf {
        a.journal().inject(JournalFault {
            transaction: f.tx.into(),
            stage: match f.stage {
                JStage::Begin => FaultStage::Begin,
                JStage::Body => FaultStage::Body,
                JStage::CommitLost => FaultStage::CommitLost,
                JStage::CommitDone => FaultStage::CommitDone,
            },
            code: if f.io {
                FaultCode::IoErr
            } else {
                FaultCode::Busy
            },
        });
    }
    if let Some(f) = ff {
        a.fail_once(f.step, FxError::Errno(f.errno));
    }
}

/// Disarm every injected fault (a one-shot fault nobody reached must not leak).
fn clear(a: &FsWriteAdapter<'_>) {
    a.journal().inject(JournalFault {
        transaction: "(disarmed)".into(),
        stage: FaultStage::Begin,
        code: FaultCode::Busy,
    });
    a.faults.lock().unwrap().clear();
}

fn request(env: &Env, i: usize, spec: &OpSpec) -> writ_broker::writ_kernel::json::Value {
    let key = format!("p{i}");
    match spec.mode {
        Mode::Create => env.create(&key, TARGETS[spec.target], SOURCES[spec.src]),
        Mode::Replace => env.replace(
            &key,
            TARGETS[spec.target],
            SOURCES[spec.src],
            spec.expected.as_deref().unwrap(),
        ),
    }
}

/// The hook for one sequential op: a foreign action or a crash at one boundary.
fn hook_for(
    env: &Env,
    spec: &OpSpec,
    deco: &Deco,
    step: usize,
    reg: &Registry,
) -> Option<crate::HookFn> {
    let fired = Arc::new(AtomicBool::new(false));
    match deco.clone() {
        Deco::Crash(at) => Some(Arc::new(move |b| {
            if b == at && !fired.swap(true, Ordering::SeqCst) {
                std::panic::resume_unwind(Box::new(CrashMark));
            }
        })),
        Deco::Foreign(at, act) => {
            let target = env.tree(TARGETS[spec.target]);
            let staging = env.staging();
            let link = env.tree(&format!("sub/hl-{step}"));
            let before: BTreeSet<String> = staging_files(env).into_iter().collect();
            let own = source_bytes(spec.src);
            let reg = reg.clone();
            Some(Arc::new(move |b| {
                if b != at || fired.swap(true, Ordering::SeqCst) {
                    return;
                }
                let mine = std::fs::read_dir(&staging)
                    .unwrap()
                    .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                    .find(|n| !before.contains(n))
                    .map(|n| staging.join(n));
                let f = Fid::Foreign(step);
                match act {
                    Act::SameBytes => foreign_write(&target, &own, &reg, f),
                    Act::OtherBytes => foreign_write(&target, FOREIGN, &reg, f),
                    Act::Delete => {
                        let _ = std::fs::remove_file(&target);
                    }
                    Act::SwapStaging => {
                        if let Some(p) = mine {
                            let bytes = std::fs::read(&p).unwrap();
                            foreign_write(&p, &bytes, &reg, f);
                        }
                    }
                    Act::LinkTarget => {
                        if target.exists() {
                            std::fs::hard_link(&target, &link).unwrap();
                        }
                    }
                    Act::LinkStaging => {
                        if let Some(p) = mine {
                            std::fs::hard_link(&p, &link).unwrap();
                        }
                    }
                }
            }))
        }
        _ => None,
    }
}

fn permutations(n: usize) -> Vec<Vec<usize>> {
    if n == 0 {
        return vec![vec![]];
    }
    let mut out = Vec::new();
    for p in permutations(n - 1) {
        for k in 0..=p.len() {
            let mut q = p.clone();
            q.insert(k, n - 1);
            out.push(q);
        }
    }
    out
}

/// Events in serialization order for the final per-target check.
#[derive(Clone, Copy, Debug)]
enum Event {
    Op(usize),
    Foreign(usize),
}

/// What a case covered (for the report).
#[derive(Default, Debug)]
struct Stats {
    ops: usize,
    groups: usize,
    crashes: usize,
    foreign: usize,
    jfaults: usize,
    ffaults: usize,
    rows: BTreeMap<String, usize>,
    disabled: usize,
}

/// Run one case; `Err` names the first violation.
fn run_case(tag: &str, seed: u64, p: &Profile) -> Result<Stats, String> {
    let env = Env::new(&format!("prop-{tag}-{seed}"));
    let r = run_in(&env, seed, p, None);
    MUTANTS.lock().unwrap().remove(&env.cfg.journal);
    r
}

fn run_in(env: &Env, seed: u64, p: &Profile, kill: Option<usize>) -> Result<Stats, String> {
    let reg: Registry = Arc::new(Mutex::new(BTreeMap::new()));
    place_initial(env, &reg);
    let mut m = Model::new();
    let mut rng = Rng(seed);
    let mut stats = Stats::default();
    let mut events: Vec<Event> = Vec::new();
    let mut step = 0usize;
    let mut finishing = false;
    let mut last: Option<Snap> = None;
    let ctx = |step: usize, what: &str| format!("seed {seed} step {step}: {what}");
    'life: loop {
        let s = env.stores();
        let opened = env.adapter(&s);
        let model_open = m.open();
        let a = match (opened, model_open) {
            (Ok(a), Ok(())) => a,
            (Err(e), Err(code)) if e.code() == code => {
                if !m.disabled {
                    return Err(ctx(step, &format!("open refused {code} unexpectedly")));
                }
                stats.disabled += 1;
                return Ok(stats);
            }
            (r, want) => {
                return Err(ctx(
                    step,
                    &format!(
                        "open: real {:?} model {want:?}",
                        r.as_ref().err().map(|e| e.code())
                    ),
                ))
            }
        };
        let mut prev = observe(env, &a, &mut m, &reg);
        let want = m.snap();
        if prev != want {
            return Err(ctx(step, &format!("after open\n  {}", diff(&want, &prev))));
        }
        // Recovery and garbage collection at open obey the invariants too.
        if let Some(before) = &last {
            invariants(before, &prev, &m).map_err(|e| ctx(step, &format!("at open: {e}")))?;
        }
        last = Some(prev.clone());
        let b = broker(&s, &a);
        if finishing {
            return finish(env, &s, &a, &b, &mut m, &reg, &events, stats)
                .map_err(|e| ctx(step, &e));
        }
        while step < p.steps {
            if m.disabled {
                break;
            }
            let kind = gen_step(&mut rng, &m, p);
            let this = step;
            step += 1;
            match kind {
                StepKind::Reopen => continue 'life,
                StepKind::Recover(jf, ff) => {
                    stats.jfaults += usize::from(jf.is_some());
                    stats.ffaults += usize::from(ff.is_some());
                    arm(&a, jf, ff);
                    (m.jf, m.ff) = (jf, ff);
                    let real = a.recover();
                    let want = m.recover(None);
                    clear(&a);
                    (m.jf, m.ff) = (None, None);
                    let real = real
                        .map(|rows| {
                            rows.into_iter()
                                .map(|(id, r)| {
                                    let i =
                                        m.ops.iter().position(|o| o.op_id.as_deref() == Some(&id));
                                    (i.unwrap_or(usize::MAX), r)
                                })
                                .collect::<Vec<_>>()
                        })
                        .map_err(|f| {
                            if f.error.code() == crate::ROOT_UNRECOVERABLE {
                                Res::ErrUnrec
                            } else {
                                Res::ErrOther
                            }
                        });
                    if real != want {
                        return Err(ctx(this, &format!("recover: real {real:?} model {want:?}")));
                    }
                    if let Ok(rows) = &real {
                        for (_, r) in rows {
                            *stats.rows.entry(format!("recover {r}")).or_default() += 1;
                        }
                    }
                }
                StepKind::Seq(spec, deco, xj, xf) => {
                    let i = m.ops.len();
                    m.ops.push(MOp {
                        spec: spec.clone(),
                        op_id: None,
                    });
                    stats.ops += 1;
                    events.push(Event::Op(i));
                    let req = request(env, i, &spec);
                    let (jf, ff) = match &deco {
                        Deco::J(f) => (Some(*f), None),
                        Deco::F(f) => (None, Some(*f)),
                        _ => (xj, xf),
                    };
                    stats.jfaults += usize::from(jf.is_some());
                    stats.ffaults += usize::from(ff.is_some());
                    arm(&a, jf, ff);
                    (m.jf, m.ff) = (jf, ff);
                    let crash = match deco {
                        Deco::Crash(at) => Some(at),
                        _ => None,
                    };
                    if let Deco::Foreign(at, act) = deco {
                        m.foreign = Some((at, act, this));
                        stats.foreign += 1;
                        events.push(Event::Foreign(spec.target));
                    }
                    m.crash = crash;
                    if kill == Some(this) {
                        // SIGKILL property: the parent kills this child at the hook.
                        let at = crash.unwrap();
                        a.set_hook(Some(Arc::new(move |bd| {
                            if bd == at {
                                say_and_hang(at);
                            }
                        })));
                        say("READY");
                        let _ = exec(&b, &req);
                        say("FINISHED");
                        return Ok(stats);
                    }
                    a.set_hook(hook_for(env, &spec, &deco, this, &reg));
                    let real =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| exec(&b, &req)));
                    let want = m.apply(i);
                    (m.jf, m.ff, m.foreign, m.crash) = (None, None, None, None);
                    match real {
                        Err(payload) => {
                            if !payload.is::<CrashMark>() {
                                std::panic::resume_unwind(payload);
                            }
                            if want != Res::Crashed {
                                return Err(ctx(this, &format!("op {i}: crashed, model {want:?}")));
                            }
                            stats.crashes += 1;
                            // A process restart: everything is dropped and reopened.
                            continue 'life;
                        }
                        Ok(e) => {
                            a.set_hook(None);
                            clear(&a);
                            let got = class(&e);
                            if got != want {
                                return Err(ctx(
                                    this,
                                    &format!("op {i} {spec:?}: real {got:?} model {want:?}"),
                                ));
                            }
                        }
                    }
                }
                StepKind::Group(specs, jf, ff) => {
                    stats.groups += 1;
                    stats.jfaults += usize::from(jf.is_some());
                    stats.ffaults += usize::from(ff.is_some());
                    let first = m.ops.len();
                    for spec in &specs {
                        m.ops.push(MOp {
                            spec: spec.clone(),
                            op_id: None,
                        });
                    }
                    stats.ops += specs.len();
                    let reqs: Vec<_> = specs
                        .iter()
                        .enumerate()
                        .map(|(k, spec)| request(env, first + k, spec))
                        .collect();
                    arm(&a, jf, ff);
                    let barrier = Barrier::new(reqs.len());
                    let results: Vec<Res> = std::thread::scope(|sc| {
                        let hs: Vec<_> = reqs
                            .iter()
                            .map(|req| {
                                let (b, barrier) = (&b, &barrier);
                                sc.spawn(move || {
                                    barrier.wait();
                                    class(&exec(b, req))
                                })
                            })
                            .collect();
                        hs.into_iter().map(|h| h.join().unwrap()).collect()
                    });
                    clear(&a);
                    let real = observe(env, &a, &mut m, &reg);
                    let mut matched = None;
                    let mut first_diff = String::new();
                    for perm in permutations(specs.len()) {
                        let mut mm = m.clone();
                        (mm.jf, mm.ff) = (jf, ff);
                        let mut want = vec![Res::Unexpected(String::new()); specs.len()];
                        for k in &perm {
                            want[*k] = mm.apply(first + k);
                        }
                        (mm.jf, mm.ff) = (None, None);
                        let snap = mm.snap();
                        if want == results && snap == real {
                            matched = Some((mm, perm));
                            break;
                        }
                        if first_diff.is_empty() {
                            first_diff = format!(
                                "perm {perm:?}: real {results:?} model {want:?}\n  {}",
                                diff(&snap, &real)
                            );
                        }
                    }
                    let Some((mm, perm)) = matched else {
                        return Err(ctx(
                            this,
                            &format!(
                                "group {specs:?} jf {jf:?} ff {ff:?}: no serialization matches; {first_diff}"
                            ),
                        ));
                    };
                    m = mm;
                    events.extend(perm.iter().map(|k| Event::Op(first + k)));
                }
            }
            let now = observe(env, &a, &mut m, &reg);
            let want = m.snap();
            if now != want {
                return Err(ctx(this, &format!("state\n  {}", diff(&want, &now))));
            }
            invariants(&prev, &now, &m).map_err(|e| ctx(this, &e))?;
            last = Some(now.clone());
            prev = now;
        }
        if m.disabled {
            drop(b);
            drop(a);
            drop(s);
            let s = env.stores();
            let code = env.adapter(&s).err().map(|e| e.code().to_string());
            if code.as_deref() != Some(crate::ROOT_UNRECOVERABLE) {
                return Err(ctx(step, &format!("disabled root reopened: {code:?}")));
            }
            stats.disabled += 1;
            return Ok(stats);
        }
        finishing = true;
    }
}

/// The final checks after the last recovery (module documentation).
#[allow(clippy::too_many_arguments)]
fn finish(
    env: &Env,
    s: &Stores,
    a: &FsWriteAdapter<'_>,
    b: &writ_broker::execute::Broker<
        '_,
        writ_ledger_sqlite::SqliteLedger,
        writ_ledger_sqlite::SqliteEffectStore,
    >,
    m: &mut Model,
    reg: &Registry,
    events: &[Event],
    mut stats: Stats,
) -> Result<Stats, String> {
    let snap = observe(env, a, m, reg);
    // Recovery final state against the serialization of the committed operations.
    let mut expect: BTreeMap<usize, Option<Option<Fid>>> = (0..TARGETS.len())
        .map(|t| (t, Some((t == 1).then_some(Fid::Foreign(INIT_STEP)))))
        .collect();
    for ev in events {
        match *ev {
            Event::Foreign(t) => {
                expect.insert(t, None);
            }
            Event::Op(i) => {
                let t = m.ops[i].spec.target;
                let rec = snap.recs.get(&i);
                match rec.and_then(|r| r.out.as_ref()).map(|o| o.result.as_str()) {
                    Some("applied") => {
                        let r = rec.unwrap();
                        if let Some(e) = expect[&t] {
                            if r.prior != e {
                                return Err(format!(
                                    "serialization: op {i} applied over {:?}, expected {e:?}",
                                    r.prior
                                ));
                            }
                        }
                        expect.insert(t, Some(Some(Fid::Op(i))));
                    }
                    Some("failed") => {}
                    // unknown, unresolved, or nothing recorded (a crash before T-intent
                    // committed, an unrecoverable root, an unknown commit).
                    Some(_) => {
                        expect.insert(t, None);
                    }
                    None => {
                        if rec.is_some() {
                            expect.insert(t, None);
                        }
                    }
                }
            }
        }
    }
    for (t, e) in expect {
        if let Some(e) = e {
            let real = snap.names.get(&Name::Target(t)).map(|x| x.0);
            if real != e {
                return Err(format!(
                    "serialization: target {t} is {real:?}, the committed operations leave {e:?}"
                ));
            }
        }
    }
    // Reconciliation: receipts follow the journal; repeated reconciliation changes nothing.
    b.reconcile()
        .map_err(|r| format!("reconcile {}", r.code()))?;
    let again = b
        .reconcile()
        .map_err(|r| format!("reconcile {}", r.code()))?;
    if again.changed() != 0 {
        return Err("a repeated reconciliation changed receipts".into());
    }
    for (i, op) in m.ops.iter().enumerate() {
        let key = format!("p{i}");
        let status = status(s, &key).unwrap_or_default();
        let result = snap
            .recs
            .get(&i)
            .and_then(|r| r.out.as_ref())
            .map(|o| o.result.clone());
        let want = match result.as_deref() {
            Some("applied") => "observed_success",
            Some("failed") => "observed_failure",
            _ => "unknown_reconcile",
        };
        if status != want {
            return Err(format!("op {i}: receipt {status}, journal {result:?}"));
        }
        if let Some(o) = snap.recs.get(&i).and_then(|r| r.out.as_ref()) {
            *stats
                .rows
                .entry(format!("{} {}", o.result, o.reason))
                .or_default() += 1;
        }
        if let Some(id) = &op.op_id {
            let permit = id.strip_prefix("fsop-").unwrap();
            let ev = a.evidence(permit).map_err(|f| f.error.code().to_string())?;
            if ev.is_some() != matches!(result.as_deref(), Some("applied" | "failed")) {
                return Err(format!("op {i}: evidence {ev:?} for result {result:?}"));
            }
        }
        // No retry is ever redispatched, whatever the outcome.
        let retry = exec(b, &request(env, i, &op.spec));
        if retry.permit.is_some() {
            return Err(format!("op {i}: a retry was dispatched again ({result:?})"));
        }
    }
    Ok(stats)
}

// ---------------------------------------------------------------------------------------
// Tests.

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

const CI: Profile = Profile {
    cases: 40,
    steps: 14,
    sequential_only: false,
};

const SEED: u64 = 20_261_116;

#[test]
fn co_prop_01_fs_write_model_matches_under_faults_crashes_and_concurrency() {
    let p = Profile {
        cases: env_usize("WRIT_FSFX_PROP_CASES", CI.cases),
        ..CI
    };
    let seed = std::env::var("WRIT_FSFX_PROP_SEED")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(SEED);
    let started = std::time::Instant::now();
    let mut total = Stats::default();
    for c in 0..p.cases {
        let st =
            run_case("main", seed.wrapping_add(c as u64), &p).unwrap_or_else(|e| panic!("{e}"));
        total.ops += st.ops;
        total.groups += st.groups;
        total.crashes += st.crashes;
        total.foreign += st.foreign;
        total.jfaults += st.jfaults;
        total.ffaults += st.ffaults;
        total.disabled += st.disabled;
        for (k, v) in st.rows {
            *total.rows.entry(k).or_default() += v;
        }
    }
    eprintln!(
        "CO-PROP-01 fs.write: seed {seed} cases {} in {:?}: {total:?}",
        p.cases,
        started.elapsed()
    );
    assert!(total.groups > 0 && total.crashes > 0 && total.foreign > 0);
    assert!(total.jfaults > 0 && total.ffaults > 0);
}

/// Each mutant must be rejected within a bounded number of seeded cases.
fn mutant_caught(mutant: Mutant, tag: &str) {
    let p = CI;
    for c in 0..400u64 {
        let seed = SEED.wrapping_add(1_000 + c);
        let env = Env::new(&format!("prop-{tag}-{seed}"));
        MUTANTS
            .lock()
            .unwrap()
            .insert(env.cfg.journal.clone(), mutant);
        let r = run_in(&env, seed, &p, None);
        MUTANTS.lock().unwrap().remove(&env.cfg.journal);
        if let Err(e) = r {
            eprintln!("mutant {mutant:?} rejected after {} cases: {e}", c + 1);
            return;
        }
    }
    panic!("mutant {mutant:?} survived 400 cases");
}

#[test]
fn mutant_content_equality_as_completion_is_rejected() {
    mutant_caught(Mutant::ContentEquality, "m-content");
}

#[test]
fn mutant_release_block_early_is_rejected() {
    mutant_caught(Mutant::ReleaseBlockEarly, "m-block");
}

#[test]
fn mutant_gc_tainted_is_rejected() {
    mutant_caught(Mutant::GcTainted, "m-gc");
}

#[test]
fn mutant_skip_fsync_mark_is_rejected() {
    mutant_caught(Mutant::SkipFsyncMark, "m-fsync");
}

// ---------------------------------------------------------------------------------------
// SIGKILL property (real process kills; run with the kill tests, alone).

const KILL_CHILD: &str = "WRIT_FSFX_PROP_KILL";

fn say(line: &str) {
    use std::io::Write;
    let out = std::io::stdout();
    let mut out = out.lock();
    writeln!(out, "{line}").unwrap();
    out.flush().unwrap();
}

fn say_and_hang(at: Boundary) -> ! {
    say(&format!("AT {at:?}"));
    loop {
        std::thread::sleep(std::time::Duration::from_secs(60));
    }
}

/// Child entry point: inert unless the parent set the environment variable.
#[test]
fn prop_kill_child() {
    let Ok(spec) = std::env::var(KILL_CHILD) else {
        return;
    };
    let parts: Vec<&str> = spec.split('|').collect();
    let env = Env::at(Path::new(parts[0]), false);
    let seed: u64 = parts[1].parse().unwrap();
    let kill: usize = parts[2].parse().unwrap();
    run_in(&env, seed, &KILL, Some(kill)).unwrap();
}

const KILL: Profile = Profile {
    cases: 40,
    steps: 10,
    sequential_only: true,
};

/// The first crash step of a sequential-only case and its boundary, from a model-only
/// replay of the generator (the parent never executes the prefix itself).
fn kill_plan(seed: u64) -> Option<(usize, Boundary, Model)> {
    let mut m = Model::new();
    m.open().ok()?;
    let mut rng = Rng(seed);
    for step in 0..KILL.steps {
        if m.disabled {
            return None;
        }
        match gen_step(&mut rng, &m, &KILL) {
            StepKind::Reopen => m.open().ok()?,
            StepKind::Recover(..) => {
                // Model-only: op ids are unknown, but without faults the order is immaterial.
                m.recover(None).ok()?;
            }
            StepKind::Group(..) => return None,
            StepKind::Seq(spec, deco, xj, xf) => {
                let i = m.ops.len();
                m.ops.push(MOp { spec, op_id: None });
                (m.jf, m.ff) = (xj, xf);
                match deco {
                    Deco::Crash(at) => {
                        m.crash = Some(at);
                        let r = m.apply(i);
                        (m.jf, m.ff, m.crash) = (None, None, None);
                        if r != Res::Crashed {
                            return None;
                        }
                        return Some((step, at, m));
                    }
                    // Foreign identities cannot cross the process boundary.
                    Deco::Foreign(..) => return None,
                    Deco::J(f) => m.jf = Some(f),
                    Deco::F(f) => m.ff = Some(f),
                    Deco::None => {}
                }
                m.apply(i);
                (m.jf, m.ff) = (None, None);
            }
        }
    }
    None
}

#[test]
#[ignore = "real process kills: run alone with --ignored --test-threads=1"]
fn sigkill_property() {
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Command, Stdio};
    let mut ran = 0;
    let mut seed = SEED.wrapping_add(50_000);
    while ran < KILL.cases {
        seed += 1;
        let Some((kill, at, mut m)) = kill_plan(seed) else {
            continue;
        };
        ran += 1;
        let env = Env::new(&format!("prop-kill-{seed}"));
        let reg: Registry = Arc::new(Mutex::new(BTreeMap::new()));
        place_initial(&env, &reg);
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "prop_tests::prop_kill_child",
                "--exact",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(KILL_CHILD, format!("{}|{seed}|{kill}", env.dir.display()))
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let want = format!("AT {at:?}");
        loop {
            match lines.next() {
                Some(Ok(l)) if l.ends_with(&want) => break,
                Some(Ok(l)) if l == "FINISHED" => {
                    let _ = child.kill();
                    panic!("seed {seed}: child finished without reaching {at:?}");
                }
                Some(Ok(_)) => {}
                _ => {
                    let _ = child.kill();
                    panic!("seed {seed}: child ended before {at:?}");
                }
            }
        }
        child.kill().unwrap();
        assert_eq!(child.wait().unwrap().signal(), Some(9));
        // Recovery after the kill, against the model's prediction.
        let s = env.stores();
        let a = env.adapter(&s).unwrap();
        // Op ids from the journal (needed for nothing but the report), then the model's open.
        let _ = observe(&env, &a, &mut m, &reg);
        m.open().unwrap();
        let real = observe(&env, &a, &mut m, &reg);
        let want = m.snap();
        assert!(
            real == want,
            "seed {seed} killed at step {kill} {at:?}:\n  {}",
            diff(&want, &real)
        );
        let b = broker(&s, &a);
        let events: Vec<Event> = (0..m.ops.len()).map(Event::Op).collect();
        finish(&env, &s, &a, &b, &mut m, &reg, &events, Stats::default())
            .unwrap_or_else(|e| panic!("seed {seed}: {e}"));
        eprintln!("CO-PROP-01 SIGKILL: seed {seed} killed at step {kill} {at:?}, recovery matched");
    }
}
