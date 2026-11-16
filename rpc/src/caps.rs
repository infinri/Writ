//! Connection caps (J-2): a global cap and a per-uid cap on open connections. A slot is
//! taken right after admission and released when the connection ends; a connection over a
//! cap is closed at once without reading.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, PoisonError};

#[derive(Debug, Default)]
struct Counts {
    total: usize,
    per_uid: BTreeMap<u32, usize>,
}

/// Shared open-connection accounting.
#[derive(Clone, Debug)]
pub struct ConnectionCaps {
    global: usize,
    per_uid: usize,
    counts: Arc<Mutex<Counts>>,
}

/// One open connection's slot; dropping it releases the slot.
#[derive(Debug)]
pub struct CapSlot {
    uid: u32,
    counts: Arc<Mutex<Counts>>,
}

impl ConnectionCaps {
    /// Caps of `global` open connections in all and `per_uid` per uid. `None` when either is
    /// zero or `per_uid > global`.
    pub fn new(global: usize, per_uid: usize) -> Option<ConnectionCaps> {
        (global > 0 && per_uid > 0 && per_uid <= global).then(|| ConnectionCaps {
            global,
            per_uid,
            counts: Arc::new(Mutex::new(Counts::default())),
        })
    }

    /// A slot for `uid`, or `None` at either cap.
    pub fn try_acquire(&self, uid: u32) -> Option<CapSlot> {
        let mut c = self.counts.lock().unwrap_or_else(PoisonError::into_inner);
        let mine = c.per_uid.get(&uid).copied().unwrap_or(0);
        if c.total >= self.global || mine >= self.per_uid {
            return None;
        }
        c.total += 1;
        c.per_uid.insert(uid, mine + 1);
        Some(CapSlot {
            uid,
            counts: Arc::clone(&self.counts),
        })
    }

    /// Open connections (all uids).
    pub fn open(&self) -> usize {
        self.counts
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .total
    }
}

impl Drop for CapSlot {
    fn drop(&mut self) {
        let mut c = self.counts.lock().unwrap_or_else(PoisonError::into_inner);
        c.total = c.total.saturating_sub(1);
        if let Some(n) = c.per_uid.get_mut(&self.uid) {
            *n = n.saturating_sub(1);
            if *n == 0 {
                c.per_uid.remove(&self.uid);
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn per_uid_and_global_caps_hold_and_release() {
        let caps = ConnectionCaps::new(3, 2).unwrap();
        let a1 = caps.try_acquire(1).unwrap();
        let _a2 = caps.try_acquire(1).unwrap();
        assert!(caps.try_acquire(1).is_none(), "per-uid cap");
        let _b1 = caps.try_acquire(2).unwrap();
        assert!(caps.try_acquire(3).is_none(), "global cap");
        assert_eq!(caps.open(), 3);
        drop(a1);
        assert_eq!(caps.open(), 2);
        assert!(caps.try_acquire(3).is_some());
    }

    #[test]
    fn invalid_caps_are_refused() {
        assert!(ConnectionCaps::new(0, 0).is_none());
        assert!(ConnectionCaps::new(1, 0).is_none());
        assert!(ConnectionCaps::new(1, 2).is_none());
    }
}
