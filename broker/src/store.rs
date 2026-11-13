//! Ledger store interface (M3-01).
//!
//! Every store error is fail closed: the broker answers the affected operation
//! (or, at startup, the whole instance) `LEDGER_UNAVAILABLE`. Operator
//! diagnostics carry a fixed category and a code only, never a path, file name,
//! key or SQL text (D-025).

use std::fmt;

use crate::clock::ClockEvent;
use crate::grants::{GrantInsert, NewGrant, ReserveOutcome, ReserveRequest};
use crate::refusal::Refusal;
/// The kernel timestamp used by the trusted-time operations.
pub use writ_kernel::time::Timestamp;

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
///
/// Every state-changing method is one fenced logical transaction: the implementation
/// checks inside the transaction that it still owns the ledger and fails closed with
/// [`StoreError::Fenced`] otherwise. Implementations expose no raw connection or
/// transaction to callers. Logical transactions for M3-02..M3-05 are added here as
/// they are implemented.
pub trait LedgerStore {
    /// The fencing epoch this instance acquired when it took ownership.
    fn owner_epoch(&self) -> u64;

    /// Append one refusal audit record (the `audit` logical transaction, D-024 rule 2).
    fn record_refusal(&mut self, code: &str) -> Result<(), StoreError>;

    /// The persisted trusted-time high-water mark `H`; `None` before the first
    /// observation (M3-TRUSTED-TIME rule 4). A read: not fenced.
    fn time_floor(&self) -> Result<Option<Timestamp>, StoreError>;

    /// Raise the high-water mark to `at` if `at` is later (the `time_floor`
    /// logical transaction, fenced). Never lowers it; returns the stored maximum.
    fn raise_time_floor(&mut self, at: Timestamp) -> Result<Timestamp, StoreError>;

    /// Append one clock audit record (the `clock_event` logical transaction, fenced).
    fn record_clock_event(&mut self, event: &ClockEvent) -> Result<(), StoreError>;
}

/// Approval grants and the T-reserve logical transaction (M3-03), on a ledger
/// shared by many threads of the owning process.
///
/// Every method takes `&self` and is one fenced `BEGIN IMMEDIATE` logical
/// transaction; implementations serialize or fence concurrent callers
/// themselves and must be safe to call from many threads at once (`Sync`). The
/// grant and comparison semantics are the broker's: the store reads the stored
/// rows, hands them to [`ReserveRequest::existing`] and
/// [`ReserveRequest::check_grant`] inside the transaction, and writes what they
/// decide. Arguments ([`NewGrant`], [`ReserveRequest`]) can only be built by
/// the broker, the latter only from a guard-bound
/// [`crate::authorize::Authorized`], so a store cannot be driven with a
/// detached authorization result.
///
/// The broker also needs the [`LedgerStore`] operations (trusted-time floor,
/// refusal audit) on the same shared ledger: [`crate::grants::GrantService`]
/// requires `&L: LedgerStore` for its ledger type `L`.
pub trait GrantLedger: Sync {
    /// The `approve` logical transaction: insert `grant` with `uses` 0, or
    /// [`GrantInsert::LabelInUse`] (rolled back; the existing grant is never
    /// reset or replenished).
    fn insert_grant(&self, grant: &NewGrant) -> Result<GrantInsert, StoreError>;

    /// The `reserve` logical transaction (LEDGER-SQLITE.md T-reserve): read the
    /// receipt of `request`'s (tenant, idempotency key); if present, apply
    /// [`ReserveRequest::existing`] (only a resumed `reserved` receipt commits,
    /// writing nothing). Otherwise, with an effective grant, apply
    /// [`ReserveRequest::check_grant`] to the stored grant row, consume one use
    /// with a conditional update of exactly one row (else `GRANT_EXHAUSTED`)
    /// and insert one `grant_uses` row; insert the receipt `reserved` and its
    /// `reserve` event; commit. Every refusal rolls back. A stored grant that
    /// fails [`crate::grants::binding_intact`] (C-4) is a store error with the
    /// diagnostic code `integrity`, never a refusal and never a grant use.
    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, StoreError>;
}
