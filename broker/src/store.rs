//! Ledger store interface (M3-01).
//!
//! Every store error is fail closed: the broker answers the affected operation
//! (or, at startup, the whole instance) `LEDGER_UNAVAILABLE`. Operator
//! diagnostics carry a fixed category and a code only, never a path, file name,
//! key or SQL text (D-025).

use std::fmt;

use crate::clock::ClockEvent;
use crate::execute::{
    DispatchIntent, Dispatched, FailureRecord, Observation, PendingReceipt, Reconciled, Resolution,
};
use crate::grants::{GrantInsert, NewGrant, ReserveOutcome, ReserveRequest};
use crate::policy::ExclusiveHold;
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

    /// Diagnostic code of a receipt transition that matched no row, or a stored
    /// status outside the schema: an application state conflict, never reported
    /// as SQLite corruption (D-029).
    pub const STATE_CONFLICT: &'static str = "STATE_CONFLICT";

    /// The diagnostic code (`SQLITE_BUSY`, `io`, `STATE_CONFLICT`, ...);
    /// `OWNED_ELSEWHERE` or `FENCED` for ownership errors.
    pub fn code(&self) -> &str {
        match self {
            StoreError::OwnedElsewhere => "OWNED_ELSEWHERE",
            StoreError::Fenced { .. } => "FENCED",
            StoreError::Unavailable { code, .. } => code,
        }
    }
}

/// Where a logical transaction failed (D-026 evidence `at`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TxStage {
    /// The transaction could not begin (connection, `BEGIN IMMEDIATE`).
    Begin,
    /// A statement or check between begin and end failed (including the
    /// fencing check and state conflicts); rolled back.
    Body,
    /// `COMMIT` (or `ROLLBACK`) failed; nothing of the transaction is kept.
    Commit,
}

impl TxStage {
    /// `begin`, `body` or `commit`.
    pub fn as_str(self) -> &'static str {
        match self {
            TxStage::Begin => "begin",
            TxStage::Body => "body",
            TxStage::Commit => "commit",
        }
    }
}

/// A failed logical transaction: the stage it failed at and the sanitized
/// store error (fail closed; externally `LEDGER_UNAVAILABLE`).
///
/// `outcome_unknown` is `true` when a `COMMIT` was issued and did not report
/// success: the transaction may or may not be durable (D-035 item 4: an
/// ambiguous commit is never assumed to have committed nothing). A failure
/// at begin, in the body, or of a `ROLLBACK` never committed anything.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TxFailure {
    /// Where it failed.
    pub stage: TxStage,
    /// The sanitized error (category and code only).
    pub error: StoreError,
    /// `true` when the outcome of an issued `COMMIT` is unknown.
    pub outcome_unknown: bool,
}

impl TxFailure {
    /// A failure of `error` at `stage` that committed nothing.
    pub fn new(stage: TxStage, error: StoreError) -> TxFailure {
        TxFailure {
            stage,
            error,
            outcome_unknown: false,
        }
    }

    /// A `COMMIT` that was issued and failed: the outcome is unknown.
    pub fn commit_unknown(error: StoreError) -> TxFailure {
        TxFailure {
            stage: TxStage::Commit,
            error,
            outcome_unknown: true,
        }
    }

    /// `true` when the store reported SQLite busy (lock contention, D-026 `busy`).
    pub fn is_busy(&self) -> bool {
        self.error.code() == "SQLITE_BUSY"
    }
}

impl From<TxFailure> for StoreError {
    fn from(f: TxFailure) -> StoreError {
        f.error
    }
}

impl fmt::Display for TxFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at {}", self.error, self.stage.as_str())?;
        if self.outcome_unknown {
            write!(f, " outcome unknown")?;
        }
        Ok(())
    }
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
    /// diagnostic code `integrity`, never a refusal and never a grant use. A
    /// failure reports the stage it happened at (D-026 evidence).
    fn reserve(
        &self,
        request: &ReserveRequest<'_>,
    ) -> Result<Result<ReserveOutcome, Refusal>, TxFailure>;
}

/// The execute lifecycle after T-reserve (M3-04, D-035; LEDGER-SQLITE.md
/// T-dispatch, T-observe, reconcile), on the same shared ledger as
/// [`GrantLedger`].
///
/// Each method is one fenced `BEGIN IMMEDIATE` logical transaction. A receipt
/// transition that matches no row (the receipt is not in the expected status
/// or binds another fingerprint) rolls back and fails at [`TxStage::Body`]
/// with the code [`StoreError::STATE_CONFLICT`] (D-029). Arguments are built
/// only by the broker: [`DispatchIntent`] from a reservation of a guard-bound
/// authorization, [`Observation`] from a guard-bound authorization,
/// [`Resolution`] from the effect adapter's evidence, and [`ExclusiveHold`]
/// only by taking the coordination gate exclusively.
pub trait ExecuteLedger: GrantLedger {
    /// The `dispatch` logical transaction (D-035 item 1): read the intent's
    /// receipt, which must be `reserved` with the intent's fingerprint and
    /// effective grant label (otherwise `STATE_CONFLICT`). With an effective
    /// grant, revalidate it with [`DispatchIntent::revalidate`] on the stored
    /// grant row, its provenance and whether this receipt's grant use is
    /// recorded: a refusal (`GRANT_MISMATCH`, `GRANT_EXPIRED`) rolls back and
    /// keeps the receipt `reserved` with its grant use; integrity or
    /// provenance failures are store errors (codes `integrity`, `provenance`).
    /// Otherwise increment the `next_permit` counter, change the receipt to
    /// `dispatched`, record the permit number for the receipt and append its
    /// `dispatch` event; commit. Committed before any effect can start (C-3).
    fn dispatch(
        &self,
        intent: &DispatchIntent<'_>,
    ) -> Result<Result<Dispatched, Refusal>, TxFailure>;

    /// The `observe` logical transaction: change the receipt from `dispatched`
    /// to `observed_success` or `observed_failure` (exactly one row), append
    /// its `observe` event; commit.
    fn observe_effect(&self, observation: &Observation<'_>) -> Result<(), TxFailure>;

    /// Read, under the exclusive coordination gate, every `dispatched` or
    /// `unknown_reconcile` receipt with the permit number recorded at its
    /// dispatch (`None` when none is recorded). A fenced read-only transaction
    /// named `reconcile_scan`.
    fn pending_receipts(&self, gate: &ExclusiveHold<'_>) -> Result<Vec<PendingReceipt>, TxFailure>;

    /// The `reconcile` logical transaction, under the exclusive coordination
    /// gate (D-035 item 3). For each resolution with adapter evidence: a
    /// checked transition of the receipt (same fingerprint, status
    /// `dispatched` or `unknown_reconcile`, the evidence's permit number
    /// recorded for it) to `observed_success` or `observed_failure`, a
    /// `reconcile` event and a reconciliation record. For each resolution
    /// without evidence: a `dispatched` receipt becomes `unknown_reconcile`
    /// (event and record); an `unknown_reconcile` one is unchanged. Any other
    /// `dispatched` receipt also becomes `unknown_reconcile`. It never creates
    /// a receipt, never redispatches and never marks an effect completed
    /// without adapter evidence.
    fn reconcile(
        &self,
        gate: &ExclusiveHold<'_>,
        resolutions: &[Resolution],
    ) -> Result<Reconciled, TxFailure>;

    /// The `audit` logical transaction for a refusal caused by a failed
    /// lifecycle transaction: the refusal code plus the failure record (the
    /// failed transaction, its stage, the sanitized category and code, the
    /// durable progress and whether a commit outcome is unknown; D-035 item 4).
    fn record_failure(&self, code: &str, record: &FailureRecord) -> Result<(), StoreError>;
}
