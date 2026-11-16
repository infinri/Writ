//! The `fs.write` effect adapter (M5a-09; D-045 M5-D8; D-035 items 2 to 4; D-046 Q-3;
//! [M5-INTERFACES](../../docs/implementation/M5-INTERFACES.md) J-7 revision 3, J-11.9).
//!
//! [`FsWriteAdapter`] writes one file from an immutable snapshot into one configured root
//! `R` (`R/staging` 0700 and the published tree `R/tree`), under the non-transactional
//! [`EffectAdapter`] contract (amended in `writ_broker::effects`): durable intent and permit
//! consumption precede the filesystem effect, durable outcome evidence follows it, an unknown
//! outcome stays unknown forever and is never redispatched or inferred from content.
//!
//! **Open** ([`FsWriteAdapter::open`], each step fails closed before the next): unrecoverable
//! marks for the current boot (the `/run/writ/state` file mark, an in-process record of a
//! mark that could not be written, the journal's `root_state`; N3); the root `R` opened
//! without following symlinks, owned by the service uid, not group- or other-writable, no
//! access or default ACL, ext4 or xfs (tmpfs, network, FUSE and overlay refuse), no casefold;
//! the operator-recorded filesystem UUID resolved through `/dev/disk/by-uuid` to `R`'s
//! `st_dev` (any failure refuses, L-c); `staging` (0700, no group or other bits) and `tree`
//! opened beneath `R` on the same mount and device; the exclusive `flock` on `staging` held
//! for the adapter lifetime (F11); no overlap with another configured root (`..` walk, F11);
//! the journal (`writ-fseffects-1`, bound to the ledger and the root identity, `.owner`
//! lock, owner epoch); recovery of every unresolved op, then garbage collection of staging
//! files (J-7.5 cleanup rule), all under the adapter-wide lock.
//!
//! **Apply** ([`EffectAdapter::apply`], module `apply`): T-intent, stage, T-staged, recheck,
//! `renameat2` (`RENAME_NOREPLACE` for `create_only`, `RENAME_EXCHANGE` for `replace_if`),
//! durability, verify, T-outcome, cleanup, with the J-7.5 results and crash boundaries
//! B0..B12. The adapter-wide lock and the op's live-set entry are acquired and released by
//! the thread executing `apply` (M6). Re-observation inside `apply` runs the recovery routine
//! with the guard `apply` holds.
//!
//! **Recovery** (module `recover`): the J-7.6 decision table, rows in their binding order
//! (R9, then by journal state; for `staged` ops the unsafe rows R8, R4, R3y, R6 before the
//! completing rows R3, R3x, R5, then R7 and R-other). `resolved` ops are never revisited.
//!
//! **Evidence** ([`EffectAdapter::evidence`], [`EffectAdapter::inspect`]): only for resolved
//! ops with result `applied` or `failed` (J-7.7); an unresolved or `unknown` op yields none.
//!
//! **Unrecoverable root** (N3): any SQLite I/O error (`SQLITE_IOERR`, `SQLITE_FULL`,
//! `SQLITE_CORRUPT`) on the journal and any `fsync` error disables the adapter until reboot:
//! the mark file first, then `root_state` if the journal still accepts a write; every later
//! operation answers `TxStage::Begin` `ROOT_UNRECOVERABLE` before any permit check.
//!
//! **Dispositions** (module `dispose`, J-11.9): the journal side of `fs_target_release`
//! (context with the `writ-fs-target-state/1` digest and worker-gone evidence, the idempotent
//! journal step keyed by a unique `disposition_id`, crash completion). The Operator ceremony
//! and the ledger `dispositions` table are not in this crate (lead-owned schema change;
//! [`dispose::DispositionLedger`] is the interface it will implement).
//!
//! **Limitations (recorded):** the inode generation is not read; `fs_uuid` is verified by the
//! `by-uuid` resolution, not `FS_IOC_GETFSUUID`; `has_journal` (ext4) is operator-recorded and
//! probed (J-9 P-FS-TYPE), not re-read here.

pub mod apply;
pub mod dispose;
pub mod ident;
pub mod recover;

use std::collections::BTreeSet;
use std::os::fd::OwnedFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};

use writ_broker::effects::{EffectAdapter, EffectEvidence, EffectResult, Verify};
use writ_broker::refusal::Refusal;
use writ_broker::runner::SnapshotContent;
use writ_broker::store::{StoreError, TxFailure, TxStage};
use writ_hostsys::fsfx::{self, FxError, Observed};
use writ_ledger_sqlite::{
    FsJournal, FsTx, FsTxError, JournalBinding, LedgerId, OpRow, OwnerRecord,
};

use crate::ident::{Identity, Prior};

/// Diagnostic code of a disabled root (externally `LEDGER_UNAVAILABLE`).
pub const ROOT_UNRECOVERABLE: &str = "ROOT_UNRECOVERABLE";

/// The configuration of one root (service configuration; J-7.1, J-7.2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RootConfig {
    /// Root id (`[a-z0-9][a-z0-9-]{0,62}`).
    pub root_id: String,
    /// The absolute path of `R` (under `/srv/writ/targets/` in the reference layout).
    pub path: PathBuf,
    /// The journal file.
    pub journal: PathBuf,
    /// Create a new journal (first start) instead of opening the existing one.
    pub create_journal: bool,
    /// The operator-recorded filesystem UUID of `R`.
    pub fs_uuid: String,
    /// Where filesystem UUIDs resolve (`/dev/disk/by-uuid`).
    pub by_uuid_dir: PathBuf,
    /// The boot-scoped mark directory (`/run/writ/state`, `writ:writ` 0700).
    pub state_dir: PathBuf,
    /// The service uid that must own every directory on the path.
    pub owner_uid: u32,
    /// Maximum target length in bytes.
    pub max_path_bytes: usize,
    /// Maximum target depth.
    pub max_depth: usize,
    /// Maximum size of a file the adapter reads or writes.
    pub max_file_bytes: u64,
    /// `writ.service`'s `cgroup.procs` (worker-gone evidence, J-11.9 M6); `None`: the
    /// evidence is unavailable and every disposition refuses `DISPOSITION_PRECONDITION`.
    pub service_cgroup_procs: Option<PathBuf>,
}

impl RootConfig {
    /// A configuration with the reference defaults for everything but the given values.
    pub fn new(root_id: &str, path: &Path, journal: &Path, fs_uuid: &str) -> RootConfig {
        RootConfig {
            root_id: root_id.to_string(),
            path: path.to_path_buf(),
            journal: journal.to_path_buf(),
            create_journal: false,
            fs_uuid: fs_uuid.to_string(),
            by_uuid_dir: PathBuf::from("/dev/disk/by-uuid"),
            state_dir: PathBuf::from("/run/writ/state"),
            owner_uid: fsfx::euid(),
            max_path_bytes: 1024,
            max_depth: 32,
            max_file_bytes: 64 << 20,
            service_cgroup_procs: None,
        }
    }
}

/// The crash boundaries of J-7.5 (test hooks; B1 and B5 and B11 are the moments right after
/// the respective commit, their ambiguous variants are injected journal faults).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Boundary {
    /// Inside T-intent before its commit.
    B0,
    /// After T-intent committed.
    B1,
    /// Staged file partially written.
    B2,
    /// Staged file written, before its `fsync`.
    B3,
    /// Staged and synced, before T-staged.
    B4,
    /// After T-staged committed.
    B5,
    /// Before the recheck.
    B6,
    /// After the recheck, before the rename.
    B7,
    /// After the rename, before `fsync`.
    B8,
    /// After the durability `fsync`s, before verification.
    B9,
    /// After verification, before T-outcome.
    B10,
    /// After T-outcome committed, before cleanup.
    B11,
    /// After cleanup, before `apply` returns (the broker's T-observe follows).
    B12,
    /// L-b: after `abandoned` committed, before the own file is removed.
    Abandoned,
    /// L-b: after the own file was removed, before `failed` is committed.
    AbandonUnlinked,
}

/// Behavioural mutants of the CO-PROP-01 property harness (test only, M5a-10): each one
/// disables one J-7 rule, and the harness must reject every one of them.
#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mutant {
    /// Recovery treats a `create_only` target whose bytes equal `content_digest` as completed
    /// (R3) although its identity is not the staged one (M5-D8: content is never evidence).
    ContentEquality,
    /// T-staged releases the target block before the op is resolved.
    ReleaseBlockEarly,
    /// Garbage collection ignores taint.
    GcTainted,
    /// An `fsync` error does not mark the root unrecoverable (N3).
    SkipFsyncMark,
}

/// The mutant of each journal path (test only; set before the adapter opens).
#[cfg(test)]
pub(crate) static MUTANTS: Mutex<std::collections::BTreeMap<PathBuf, Mutant>> =
    Mutex::new(std::collections::BTreeMap::new());

/// A boundary hook (test only).
#[cfg(test)]
pub(crate) type HookFn = std::sync::Arc<dyn Fn(Boundary) + Send + Sync>;

/// A filesystem step a unit test can fail once with an `errno` (test only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    /// Exclusive create of the staged file.
    Create,
    /// Writing the staged file.
    Write,
    /// `fsync` of the staged file.
    FsyncFile,
    /// `fsync` of `staging`.
    FsyncStaging,
    /// `statx` of the staged file (any error; `NoBirthTime` simulates a missing `STATX_BTIME`).
    StatxStaged,
    /// The rename.
    Rename,
    /// `fsync` of the target's parent.
    FsyncParent,
    /// Observation of the target in recovery.
    ObserveTarget,
    /// The cleanup unlink after T-outcome.
    Cleanup,
}

/// The `fs.write` adapter of one root (module documentation).
pub struct FsWriteAdapter<'s> {
    pub(crate) cfg: RootConfig,
    pub(crate) journal: FsJournal,
    pub(crate) tree: OwnedFd,
    pub(crate) staging: OwnedFd,
    pub(crate) mnt_id: u64,
    pub(crate) dev: std::sync::atomic::AtomicU64,
    pub(crate) snapshots: &'s dyn SnapshotContent,
    pub(crate) lock: Mutex<()>,
    pub(crate) live: Mutex<BTreeSet<String>>,
    /// A journal commit failed since the last (re)open: reopen before anything reads it (F6).
    pub(crate) dirty: AtomicBool,
    /// Marked unrecoverable for this boot (N3).
    pub(crate) disabled: AtomicBool,
    pub(crate) boot_id: String,
    pub(crate) staging_prefix: String,
    pub(crate) gc_kept: Mutex<Vec<String>>,
    #[cfg(test)]
    pub(crate) hook: Mutex<Option<HookFn>>,
    #[cfg(test)]
    pub(crate) faults: Mutex<Vec<(Step, FxError)>>,
    #[cfg(test)]
    pub(crate) mutant: Option<Mutant>,
    _root: OwnedFd,
    /// The adapter-lifetime `flock` on `staging` (F11); last field.
    _staging_lock: StagingLock,
}

/// The exclusive `flock` on `R/staging`, held for the adapter lifetime (F11). Like the
/// stores' `OwnerLock` (E-066), it is acquired and released under the exclusive guard of
/// the process lifecycle gate, and released by an explicit unlock before its descriptor is
/// closed: a `flock` belongs to the open file description, and a child spawned by any thread
/// holds duplicates of every descriptor until it executes (close-on-exec acts only at exec),
/// so closing the descriptor alone could leave the lock held after the adapter is gone (CI
/// run 43918426151).
struct StagingLock {
    fd: Option<OwnedFd>,
}

impl StagingLock {
    fn acquire(staging: &OwnedFd) -> Result<StagingLock, StoreError> {
        let fd = staging.try_clone().map_err(|_| refused("LOCK"))?;
        writ_broker::process::ownership_change(|| {
            if fsfx::try_lock_exclusive(&fd).map_err(|_| refused("LOCK"))? {
                Ok(StagingLock { fd: Some(fd) })
            } else {
                Err(refused("OWNED_ELSEWHERE"))
            }
        })
    }
}

impl Drop for StagingLock {
    fn drop(&mut self) {
        if let Some(fd) = self.fd.take() {
            writ_broker::process::ownership_change(|| {
                // If the unlock fails, closing still releases it once every duplicate is gone.
                let _ = fsfx::unlock(&fd);
                drop(fd);
            });
        }
    }
}

/// Roots whose unrecoverable mark could not be written in this process (N3: refuse to open
/// them again in this boot).
static UNMARKABLE: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

fn unmarkable_key(cfg: &RootConfig) -> String {
    format!("{}|{}", cfg.state_dir.display(), cfg.root_id)
}

/// A store error under the `fs_root` category.
pub(crate) fn refused(code: &str) -> StoreError {
    StoreError::Unavailable {
        category: "fs_root".into(),
        code: code.to_string(),
    }
}

/// An effect-stage failure (outcome unknown) with a sanitized code.
pub(crate) fn effect_failure(code: &str) -> TxFailure {
    TxFailure::effect_unknown(StoreError::Unavailable {
        category: "effect fs.write".into(),
        code: code.to_string(),
    })
}

fn is_io(f: &TxFailure) -> bool {
    matches!(
        f.error.code(),
        "SQLITE_IOERR" | "SQLITE_FULL" | "SQLITE_CORRUPT"
    )
}

/// Why an observation could not be made (row R9).
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ObsError {
    /// A primitive failed.
    Fx(FxError),
    /// Mount id or `st_dev` differs from the root's, or an identity changed under the read.
    Mismatch(&'static str),
}

impl ObsError {
    pub(crate) fn code(&self) -> String {
        match self {
            ObsError::Fx(e) => e.code(),
            ObsError::Mismatch(w) => format!("mismatch {w}"),
        }
    }
}

impl From<FxError> for ObsError {
    fn from(e: FxError) -> ObsError {
        ObsError::Fx(e)
    }
}

/// One observed name (target or staging name).
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Slot {
    Absent,
    Present {
        o: Observed,
        /// Content digest of a regular file within the size bound.
        digest: Option<String>,
        /// The file carries an access ACL.
        acl: bool,
    },
}

impl Slot {
    pub(crate) fn same(&self, id: &Identity) -> bool {
        matches!(self, Slot::Present { o, .. } if id.same_file(o))
    }

    pub(crate) fn nlink(&self) -> u32 {
        match self {
            Slot::Present { o, .. } => o.nlink,
            Slot::Absent => 0,
        }
    }

    pub(crate) fn digest(&self) -> Option<&str> {
        match self {
            Slot::Present { digest, .. } => digest.as_deref(),
            Slot::Absent => None,
        }
    }

    /// `true` when the slot holds the recorded prior state (identity and digest).
    pub(crate) fn is_prior(&self, prior: &Prior) -> bool {
        match prior {
            Prior::Absent => *self == Slot::Absent,
            Prior::File(id) => self.same(id) && self.digest() == Some(id.digest.as_str()),
        }
    }

    /// The identity of a regular file in this slot, with its digest.
    pub(crate) fn identity(&self, fs_uuid: &str) -> Option<Identity> {
        match self {
            Slot::Present {
                o, digest: Some(d), ..
            } if o.is_regular() => Some(Identity::of(fs_uuid, o, d)),
            _ => None,
        }
    }

    /// The recorded-state text of this slot (`absent`, an identity, or `other`).
    pub(crate) fn text(&self, fs_uuid: &str) -> String {
        match self {
            Slot::Absent => "absent".into(),
            s => s.identity(fs_uuid).map_or("other".into(), |i| i.text()),
        }
    }
}

/// The resolution of a target's parent beneath `tree`.
pub(crate) enum Parent {
    /// The parent and the leaf name.
    Found(OwnedFd, String),
    /// A parent directory is missing.
    Missing,
    /// A parent is not a safe directory (symlink, other type, owner, mode, ACL).
    Unsafe,
}

impl<'s> FsWriteAdapter<'s> {
    /// Open the adapter of `cfg` for the ledger `ledger`, reading sources from `snapshots`
    /// (the service's snapshot store), checking overlap against the other configured roots
    /// `peers` (module documentation for the order). Recovery and garbage collection run
    /// before this returns.
    pub fn open(
        cfg: &RootConfig,
        ledger: &LedgerId,
        snapshots: &'s dyn SnapshotContent,
        peers: &[PathBuf],
    ) -> Result<FsWriteAdapter<'s>, StoreError> {
        if !writ_broker::effects::valid_root_id(&cfg.root_id) {
            return Err(refused("ROOT_ID"));
        }
        let boot_id = fsfx::boot_id().map_err(|_| refused("BOOT_ID"))?;
        if UNMARKABLE
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .contains(&unmarkable_key(cfg))
        {
            return Err(refused(ROOT_UNRECOVERABLE));
        }
        check_state_dir(cfg)?;
        if mark_file_boot(cfg).as_deref() == Some(boot_id.as_str()) {
            return Err(refused(ROOT_UNRECOVERABLE));
        }
        let root = fsfx::open_dir_path(&cfg.path).map_err(|_| refused("ROOT_OPEN"))?;
        let ro = fsfx::observe(&root).map_err(|e| refused(&format!("ROOT_STATX {}", e.code())))?;
        check_dir_facts(cfg, &ro, &root)?;
        match fsfx::fs_magic(&root).map_err(|_| refused("FS_TYPE"))? {
            fsfx::EXT4_SUPER_MAGIC | fsfx::XFS_SUPER_MAGIC => {}
            _ => return Err(refused("FS_TYPE")),
        }
        if fsfx::casefold(&root).map_err(|_| refused("CASEFOLD"))? {
            return Err(refused("CASEFOLD"));
        }
        match fsfx::block_device(&cfg.by_uuid_dir.join(&cfg.fs_uuid)) {
            Some(rdev) if rdev == ro.dev && !cfg.fs_uuid.contains('/') => {}
            _ => return Err(refused("FS_UUID")),
        }
        let staging =
            fsfx::open_dir_beneath(&root, "staging").map_err(|_| refused("LAYOUT staging"))?;
        let tree = fsfx::open_dir_beneath(&root, "tree").map_err(|_| refused("LAYOUT tree"))?;
        for (fd, name) in [(&staging, "staging"), (&tree, "tree")] {
            let o = fsfx::observe(fd).map_err(|_| refused("LAYOUT statx"))?;
            if o.mnt_id != ro.mnt_id || o.dev != ro.dev {
                return Err(refused("MOUNT"));
            }
            check_dir_facts(cfg, &o, fd)?;
            if name == "staging" && o.perm() & 0o077 != 0 {
                return Err(refused("MODE staging"));
            }
            if fsfx::casefold(fd).map_err(|_| refused("CASEFOLD"))? {
                return Err(refused("CASEFOLD"));
            }
        }
        let staging_lock = StagingLock::acquire(&staging)?;
        check_overlap(&root, &ro, peers)?;
        let binding = JournalBinding {
            ledger_id: ledger.clone(),
            root_id: cfg.root_id.clone(),
            fs_uuid: cfg.fs_uuid.clone(),
            root_ino: ro.ino,
            root_btime: format!("{}.{:09}", ro.btime_sec, ro.btime_nsec),
        };
        let pid = std::process::id();
        let owner = OwnerRecord {
            pid,
            start: fsfx::process_start(pid).unwrap_or(0),
            boot_id: boot_id.clone(),
        };
        let journal = if cfg.create_journal {
            FsJournal::create(&cfg.journal, &binding, &owner)?
        } else {
            FsJournal::open(&cfg.journal, &binding, &owner)?
        };
        let marked = journal
            .read("fs_open", |tx| tx.root_marks())
            .map_err(|f| f.error)?;
        if marked.iter().any(|(b, _)| *b == boot_id) {
            return Err(refused(ROOT_UNRECOVERABLE));
        }
        let adapter = FsWriteAdapter {
            cfg: cfg.clone(),
            journal,
            tree,
            staging,
            mnt_id: ro.mnt_id,
            dev: std::sync::atomic::AtomicU64::new(ro.dev),
            snapshots,
            lock: Mutex::new(()),
            live: Mutex::new(BTreeSet::new()),
            dirty: AtomicBool::new(false),
            disabled: AtomicBool::new(false),
            boot_id,
            staging_prefix: ledger.as_str().get(..12).unwrap_or("ledger").to_string(),
            gc_kept: Mutex::new(Vec::new()),
            #[cfg(test)]
            hook: Mutex::new(None),
            #[cfg(test)]
            faults: Mutex::new(Vec::new()),
            #[cfg(test)]
            mutant: MUTANTS
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .get(&cfg.journal)
                .copied(),
            _root: root,
            _staging_lock: staging_lock,
        };
        {
            let guard = adapter.lock();
            adapter
                .recover_locked(&guard, None)
                .map_err(|_| refused("RECOVERY"))?;
            adapter.collect_garbage(&guard).map_err(|_| refused("GC"))?;
        }
        if adapter.disabled.load(Ordering::SeqCst) {
            return Err(refused(ROOT_UNRECOVERABLE));
        }
        Ok(adapter)
    }

    /// The root id.
    pub fn root_id(&self) -> &str {
        &self.cfg.root_id
    }

    /// Staging names found at open that garbage collection kept (reported, never removed).
    pub fn gc_kept(&self) -> Vec<String> {
        self.gc_kept
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// `true` once the root is disabled until reboot (N3).
    pub fn is_disabled(&self) -> bool {
        self.disabled.load(Ordering::SeqCst)
    }

    /// An operator-requested recovery run under the adapter-wide lock (J-7.5); returns the
    /// row each unresolved op reached (`R9` when nothing could be concluded).
    pub fn recover(&self) -> Result<Vec<(String, &'static str)>, TxFailure> {
        let guard = self.lock();
        self.recover_locked(&guard, None)
    }

    pub(crate) fn lock(&self) -> MutexGuard<'_, ()> {
        self.lock.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn dev(&self) -> u64 {
        self.dev.load(Ordering::SeqCst)
    }

    /// Test only: replace the device number recorded at open (an `st_dev` change).
    #[cfg(test)]
    pub(crate) fn set_dev(&self, dev: u64) {
        self.dev.store(dev, Ordering::SeqCst);
    }

    /// Test only: call `f` at every boundary.
    #[cfg(test)]
    pub(crate) fn set_hook(&self, f: Option<HookFn>) {
        *self.hook.lock().unwrap_or_else(PoisonError::into_inner) = f;
    }

    /// Test only: fail `step` once with `e`.
    #[cfg(test)]
    pub(crate) fn fail_once(&self, step: Step, e: FxError) {
        self.faults
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((step, e));
    }

    /// Test only: `true` when this adapter runs the mutant `m`.
    #[cfg(test)]
    pub(crate) fn is_mutant(&self, m: Mutant) -> bool {
        self.mutant == Some(m)
    }

    /// Test only: the journal (fault injection and direct reads).
    #[cfg(test)]
    pub(crate) fn journal(&self) -> &FsJournal {
        &self.journal
    }

    pub(crate) fn boundary(&self, b: Boundary) {
        #[cfg(test)]
        {
            let hook = self
                .hook
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .clone();
            if let Some(h) = hook {
                h(b);
            }
        }
        let _ = b;
    }

    /// An injected failure of `step`, if armed (test only; always `Ok` otherwise).
    pub(crate) fn fault(&self, step: Step) -> Result<(), FxError> {
        #[cfg(test)]
        {
            let mut faults = self.faults.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some(i) = faults.iter().position(|(s, _)| *s == step) {
                return Err(faults.remove(i).1);
            }
        }
        let _ = step;
        Ok(())
    }

    /// Mark the root unrecoverable until reboot (N3): the file mark first, then
    /// `root_state` if the journal still accepts a write; disabled in any case.
    pub(crate) fn mark_unrecoverable(&self, reason: &str) {
        self.disabled.store(true, Ordering::SeqCst);
        if write_mark_file(&self.cfg, &self.boot_id, reason).is_err() {
            UNMARKABLE
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .insert(unmarkable_key(&self.cfg));
        }
        let _ = self.journal.write("fs_mark", |tx| {
            tx.add_root_mark(&self.boot_id, reason)?;
            Ok(writ_ledger_sqlite::FsEnd::Commit(()))
        });
    }

    /// Run a journal transaction with the N3 and F6 rules: a journal I/O error marks the
    /// root; a failed commit makes the next access reopen the journal first.
    pub(crate) fn jwrite<T>(
        &self,
        name: &str,
        body: impl FnOnce(&FsTx<'_>) -> Result<writ_ledger_sqlite::FsEnd<T>, FsTxError>,
    ) -> Result<T, TxFailure> {
        self.reopen_if_dirty()?;
        let out = self.journal.write(name, body);
        if let Err(f) = &out {
            self.after_failure(f);
        }
        out
    }

    /// A read-only journal transaction with the same rules.
    pub(crate) fn jread<T>(
        &self,
        name: &str,
        body: impl FnOnce(&FsTx<'_>) -> Result<T, FsTxError>,
    ) -> Result<T, TxFailure> {
        self.reopen_if_dirty()?;
        let out = self.journal.read(name, body);
        if let Err(f) = &out {
            self.after_failure(f);
        }
        out
    }

    fn after_failure(&self, f: &TxFailure) {
        if f.stage == TxStage::Commit {
            self.dirty.store(true, Ordering::SeqCst);
        }
        if is_io(f) {
            self.mark_unrecoverable(&format!("journal {}", f.error.code()));
        }
    }

    fn reopen_if_dirty(&self) -> Result<(), TxFailure> {
        if self.dirty.load(Ordering::SeqCst) {
            self.journal
                .reopen()
                .map_err(|e| TxFailure::new(TxStage::Begin, e))?;
            self.dirty.store(false, Ordering::SeqCst);
        }
        Ok(())
    }

    /// `fsync` with N3: an error marks the root.
    pub(crate) fn sync(&self, fd: &OwnedFd, step: Step, what: &str) -> Result<(), TxFailure> {
        match self.fault(step).and_then(|()| fsfx::fsync(fd)) {
            Ok(()) => Ok(()),
            Err(e) => {
                #[cfg(test)]
                if self.is_mutant(Mutant::SkipFsyncMark) {
                    return Err(effect_failure(&format!("fsync {what}")));
                }
                self.mark_unrecoverable(&format!("fsync {what} {}", e.code()));
                Err(effect_failure(&format!("fsync {what}")))
            }
        }
    }

    /// The outer failure of a disabled root (before any permit check).
    pub(crate) fn unrecoverable() -> TxFailure {
        TxFailure::new(TxStage::Begin, refused(ROOT_UNRECOVERABLE))
    }

    /// Check one directory reached on a path (F10, J-7.2 mount rule).
    pub(crate) fn check_dir(&self, fd: &OwnedFd) -> Result<Result<(), &'static str>, ObsError> {
        let o = fsfx::observe(fd)?;
        if o.mnt_id != self.mnt_id || o.dev != self.dev() {
            return Err(ObsError::Mismatch("mount"));
        }
        if !o.is_dir() {
            return Ok(Err("not a directory"));
        }
        if o.uid != self.cfg.owner_uid {
            return Ok(Err("owner"));
        }
        if o.perm() & 0o022 != 0 {
            return Ok(Err("mode"));
        }
        if fsfx::acls(fd)? != (false, false) {
            return Ok(Err("acl"));
        }
        Ok(Ok(()))
    }

    /// Resolve the parent of `target` beneath `tree`, one checked component at a time.
    pub(crate) fn parent(&self, target: &str) -> Result<Parent, ObsError> {
        let mut parts: Vec<&str> = target.split('/').collect();
        let leaf = parts.pop().unwrap_or_default().to_string();
        let mut dir = self.tree.try_clone().map_err(FxError::from)?;
        if let Err(_why) = self.check_dir(&dir)? {
            return Ok(Parent::Unsafe);
        }
        for part in parts {
            dir = match fsfx::open_dir_beneath(&dir, part) {
                Ok(fd) => fd,
                Err(e) if e.is_not_found() => return Ok(Parent::Missing),
                // ELOOP (symlink), ENOTDIR (another type), EXDEV (mount crossing).
                Err(FxError::Errno(40 | 20 | 18)) => return Ok(Parent::Unsafe),
                Err(e) => return Err(e.into()),
            };
            if let Err(_why) = self.check_dir(&dir)? {
                return Ok(Parent::Unsafe);
            }
        }
        Ok(Parent::Found(dir, leaf))
    }

    /// Observe `name` in `dir` (not following symlinks); a regular file within the size
    /// bound is read through a descriptor whose identity must match the `statx` result.
    pub(crate) fn slot(&self, dir: &OwnedFd, name: &str) -> Result<Slot, ObsError> {
        let Some(o) = fsfx::observe_at(dir, name)? else {
            return Ok(Slot::Absent);
        };
        if o.mnt_id != self.mnt_id || o.dev != self.dev() {
            return Err(ObsError::Mismatch("mount"));
        }
        if !o.is_regular() || o.size > self.cfg.max_file_bytes {
            return Ok(Slot::Present {
                o,
                digest: None,
                acl: false,
            });
        }
        let fd = fsfx::open_read_beneath(dir, name)?;
        let again = fsfx::observe(&fd)?;
        if again.ino != o.ino || again.btime_sec != o.btime_sec || again.btime_nsec != o.btime_nsec
        {
            return Err(ObsError::Mismatch("identity changed"));
        }
        let acl = fsfx::acls(&fd)?.0;
        let bytes = fsfx::read_to_end(fd, self.cfg.max_file_bytes)?;
        Ok(Slot::Present {
            o: again,
            digest: Some(ident::digest(&bytes)),
            acl,
        })
    }

    /// Observe the target `target`: its parent resolution and slot.
    pub(crate) fn target_slot(&self, target: &str) -> Result<(Parent, Slot), ObsError> {
        self.fault(Step::ObserveTarget)?;
        match self.parent(target)? {
            Parent::Found(dir, leaf) => {
                let s = self.slot(&dir, &leaf)?;
                Ok((Parent::Found(dir, leaf), s))
            }
            other => Ok((other, Slot::Absent)),
        }
    }

    /// The staging name of an op (`<ledger id short>-<op id>`, F11).
    pub(crate) fn staging_name(&self, op_id: &str) -> String {
        format!("{}-{op_id}", self.staging_prefix)
    }

    /// The evidence of a resolved op with result `applied` or `failed`.
    fn evidence_of(tx: &FsTx<'_>, op: &OpRow) -> Result<Option<EffectEvidence>, FsTxError> {
        let Some(outcome) = tx.outcome(&op.op_id)? else {
            return Ok(None);
        };
        let result = match outcome.result.as_str() {
            "applied" => EffectResult::Applied,
            "failed" => EffectResult::Failed,
            _ => return Ok(None),
        };
        Ok(Some(EffectEvidence {
            permit_id: op.permit_id.clone(),
            tenant_id: op.tenant_id.clone(),
            idempotency_key: op.idempotency_key.clone(),
            request_fingerprint: op.request_fingerprint.clone(),
            result,
            reason: Some(outcome.reason),
        }))
    }
}

fn check_state_dir(cfg: &RootConfig) -> Result<(), StoreError> {
    let fd = fsfx::open_dir_path(&cfg.state_dir).map_err(|_| refused("STATE_DIR"))?;
    let o = fsfx::observe(&fd).map_err(|_| refused("STATE_DIR"))?;
    if !o.is_dir() || o.uid != cfg.owner_uid || o.perm() & 0o077 != 0 {
        return Err(refused("STATE_DIR"));
    }
    Ok(())
}

fn mark_name(cfg: &RootConfig) -> String {
    format!("fs-unrecoverable-{}", cfg.root_id)
}

/// The boot id recorded in the root's mark file, if one exists. An unreadable mark counts as
/// a mark of the current boot (fail closed).
fn mark_file_boot(cfg: &RootConfig) -> Option<String> {
    let path = cfg.state_dir.join(mark_name(cfg));
    match std::fs::read_to_string(&path) {
        Ok(text) => Some(text.lines().next().unwrap_or_default().to_string()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(_) => fsfx::boot_id().ok(),
    }
}

/// Write `<boot id>\n<reason>\n` to the mark file and `fsync` it and its directory.
fn write_mark_file(cfg: &RootConfig, boot_id: &str, reason: &str) -> Result<(), FxError> {
    let dir = fsfx::open_dir_path(&cfg.state_dir)?;
    let fd = match fsfx::create_exclusive(&dir, &mark_name(cfg)) {
        Ok(fd) => fd,
        Err(e) if e.is_exists() => return Ok(()),
        Err(e) => return Err(e),
    };
    let reason: String = reason.chars().filter(|c| !c.is_control()).collect();
    fsfx::write_all(&fd, format!("{boot_id}\n{reason}\n").as_bytes())?;
    fsfx::fsync(&fd)?;
    fsfx::fsync(&dir)
}

fn check_dir_facts(cfg: &RootConfig, o: &Observed, fd: &OwnedFd) -> Result<(), StoreError> {
    if !o.is_dir() {
        return Err(refused("LAYOUT"));
    }
    if o.uid != cfg.owner_uid {
        return Err(refused("OWNER"));
    }
    if o.perm() & 0o022 != 0 {
        return Err(refused("MODE"));
    }
    if fsfx::acls(fd).map_err(|_| refused("ACL"))? != (false, false) {
        return Err(refused("ACL"));
    }
    Ok(())
}

/// The (dev, ino) chain from `fd` up to `/`.
fn ancestry(fd: &OwnedFd) -> Result<Vec<(u64, u64)>, StoreError> {
    let mut out = Vec::new();
    let mut cur = fd.try_clone().map_err(|_| refused("OVERLAP"))?;
    for _ in 0..4096 {
        let o = fsfx::observe(&cur).map_err(|_| refused("OVERLAP"))?;
        if out.last() == Some(&(o.dev, o.ino)) {
            return Ok(out);
        }
        out.push((o.dev, o.ino));
        cur = fsfx::open_parent(&cur).map_err(|_| refused("OVERLAP"))?;
    }
    Err(refused("OVERLAP"))
}

/// F11: no configured root equals, is inside, or contains another.
fn check_overlap(root: &OwnedFd, ro: &Observed, peers: &[PathBuf]) -> Result<(), StoreError> {
    let mine = ancestry(root)?;
    for peer in peers {
        let fd = fsfx::open_dir_path(peer).map_err(|_| refused("OVERLAP"))?;
        let theirs = ancestry(&fd)?;
        let Some(&peer_id) = theirs.first() else {
            return Err(refused("OVERLAP"));
        };
        if mine.contains(&peer_id) || theirs.contains(&(ro.dev, ro.ino)) {
            return Err(refused("OVERLAP"));
        }
    }
    Ok(())
}

impl EffectAdapter for FsWriteAdapter<'_> {
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        self.apply_effect(verify)
    }

    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure> {
        if self.is_disabled() {
            return Err(Self::unrecoverable());
        }
        self.jread("fs_inspect", |tx| {
            let effect = match verify() {
                Ok(e) => e,
                Err(r) => return Ok(Err(r)),
            };
            match tx.op_by_permit(effect.permit_id())? {
                None => Ok(Ok(None)),
                Some(op) => Ok(Ok(Self::evidence_of(tx, &op)?)),
            }
        })
    }

    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        if self.is_disabled() {
            return Err(Self::unrecoverable());
        }
        self.jread("fs_evidence", |tx| match tx.op_by_permit(permit_id)? {
            None => Ok(None),
            Some(op) => Self::evidence_of(tx, &op),
        })
    }
}

#[cfg(test)]
mod test_support;

#[cfg(test)]
mod adapter_tests;

#[cfg(test)]
mod row_tests;

#[cfg(test)]
mod kill_tests;

#[cfg(test)]
mod prop_tests;
