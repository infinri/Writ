//! The process lifecycle gate and ownership locks (M3-07, E-066), split out of
//! [`crate::process`] in M5a-13 (M3) so that a store can take an [`OwnerLock`] without
//! naming the module that spawns processes: `writ-serviced` spawns no child processes and
//! the dependency gate (`kernel/tools/check_deps.py`) refuses `crate::process` anywhere in
//! its reachable modules. The design (unlock before close, the gate shared by spawns and
//! exclusive for ownership changes) is documented in [`crate::process`].

use std::fs::{File, OpenOptions, TryLockError};
use std::path::Path;
use std::sync::{PoisonError, RwLock};

/// The process-wide lifecycle gate (module documentation).
static GATE: RwLock<()> = RwLock::new(());

/// Run `f` under the shared spawn guard (the spawn paths of [`crate::process`]).
pub(crate) fn spawning<T>(f: impl FnOnce() -> T) -> T {
    let _guard = GATE.read().unwrap_or_else(PoisonError::into_inner);
    f()
}

/// Run `change` (acquiring or releasing an ownership lock) under the exclusive guard of
/// the lifecycle gate, so that it never overlaps a spawn. `change` must not spawn or call
/// this function.
pub fn ownership_change<T>(change: impl FnOnce() -> T) -> T {
    let _guard = GATE.write().unwrap_or_else(PoisonError::into_inner);
    change()
}

/// Why an ownership lock was not acquired.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OwnerLockError {
    /// Another open file description holds the lock (another owner).
    Held,
    /// The lock file could not be opened or locked.
    Io,
}

/// An exclusive OS lock (`File::try_lock`) on an ownership lock file, acquired and
/// released under the exclusive guard of the lifecycle gate. Held until dropped; a store
/// keeps it as its last field so that its connection closes first.
#[derive(Debug)]
pub struct OwnerLock {
    pub(crate) file: Option<File>,
}

impl OwnerLock {
    /// Open (creating it if needed, never truncating) and lock `path` without waiting.
    pub fn acquire(path: &Path) -> Result<OwnerLock, OwnerLockError> {
        ownership_change(|| {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)
                .map_err(|_| OwnerLockError::Io)?;
            match file.try_lock() {
                Ok(()) => Ok(OwnerLock { file: Some(file) }),
                Err(TryLockError::WouldBlock) => Err(OwnerLockError::Held),
                Err(TryLockError::Error(_)) => Err(OwnerLockError::Io),
            }
        })
    }
}

impl Drop for OwnerLock {
    fn drop(&mut self) {
        if let Some(file) = self.file.take() {
            ownership_change(|| {
                // Unlock before closing: a `flock` lock belongs to the open file
                // description, so this releases it even while a child that has not yet
                // finished exec still holds a duplicate (module documentation). If it
                // fails, closing still releases it once every duplicate is gone.
                let _ = file.unlock();
                drop(file);
            });
        }
    }
}
