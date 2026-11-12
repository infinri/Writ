//! Ledger store interface (M3-01).
//!
//! Every store error is fail closed: the broker answers the affected operation
//! (or, at startup, the whole instance) `LEDGER_UNAVAILABLE`. Operator
//! diagnostics carry a fixed category and a code only, never a path, file name,
//! key or SQL text (D-025).

use std::fmt;

/// Why a ledger cannot serve.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StoreError {
    /// Another process owns this ledger (single-owner profile); this instance must not start.
    OwnedElsewhere,
    /// The stored owner epoch is not this instance's: another owner started after it.
    Fenced {
        /// Epoch this instance acquired.
        mine: u64,
        /// Epoch found in the ledger.
        stored: u64,
    },
    /// Open, initialization, verification or a transaction failed.
    Unavailable {
        /// Fixed operator category (`open`, `ownership`, `initialize`, `transaction <name>`).
        category: String,
        /// SQLite primary result code name, `io`, or `schema`.
        code: String,
    },
}

impl StoreError {
    /// The external refusal code for every store error.
    pub const EXTERNAL_CODE: &'static str = "LEDGER_UNAVAILABLE";
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::OwnedElsewhere => write!(f, "ownership OWNED_ELSEWHERE"),
            StoreError::Fenced { mine, stored } => {
                write!(f, "ownership FENCED mine={mine} stored={stored}")
            }
            StoreError::Unavailable { category, code } => write!(f, "{category} {code}"),
        }
    }
}

/// A ledger owned by this broker instance.
pub trait LedgerStore {
    /// The fencing epoch this instance acquired when it took ownership.
    fn owner_epoch(&self) -> u64;
}
