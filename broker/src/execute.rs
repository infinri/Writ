//! The execute lifecycle after T-reserve (M3-04, D-035; D-008, D-012, D-020,
//! D-025 B and C-3, D-026, D-029, D-033 item 2, D-034 policy-gate ownership;
//! m1/fixtures/broker/BROKER.md `execute`, `present_permit`, `reconcile`,
//! `restart_broker`; LEDGER-SQLITE.md T-dispatch, T-adapter, T-observe,
//! reconcile and storage failure rules).
//!
//! **Execute** ([`Broker::execute`]), in order, under one shared hold of the
//! policy gate for the whole operation (D-029 lock order: gate, per-key lock,
//! then each transaction):
//!
//! 1. Trusted time is observed (floor persisted, `TIME_UNAVAILABLE`), the
//!    request is re-decided at use time under the current policy (including
//!    its deadline); `require_approval` without a grant is
//!    `APPROVAL_REQUIRED`. Then the per-key lock is taken and held until the
//!    operation returns.
//! 2. **T-reserve** ([`crate::store::GrantLedger::reserve`]): refusal, replay,
//!    `RECONCILIATION_REQUIRED` for a `dispatched` or `unknown_reconcile`
//!    receipt, resume of a `reserved` one, or a new reservation.
//! 3. **T-dispatch** ([`ExecuteLedger::dispatch`]): the reservation's grant is
//!    revalidated in the same transaction (D-035 item 1: an expired grant or
//!    one whose binding no longer matches the current decision refuses and
//!    keeps the receipt `reserved` with its grant use); then `next_permit`
//!    incremented, the receipt `reserved` to `dispatched` (exactly one row),
//!    the permit number recorded for the receipt, a `dispatch` event;
//!    committed before any effect can start. The permit `permit-<n>` is then
//!    issued from the guard-bound authorization, expiring at
//!    `min(now + 30 s, deadline, grant expiry)` with `now` the operation's
//!    observed time.
//! 4. **T-adapter** ([`EffectAdapter::apply`]): the permit is verified inside
//!    the adapter transaction under the same hold (D-025 B order, then
//!    `PERMIT_REPLAYED`), its id consumed atomically with the effect record.
//!    The expiry check reads trusted time through [`crate::clock::observe`],
//!    so the floor that decides it is persisted first. An adapter refusal
//!    leaves the receipt `dispatched`.
//! 5. **T-observe** ([`ExecuteLedger::observe_effect`]): `dispatched` to
//!    `observed_success` or `observed_failure`.
//!
//! A refusal is audited in its own transaction after the gate and key lock
//! are released (audit failure does not change the answer). A refusal caused
//! by a failed lifecycle transaction is audited with its [`FailureRecord`].
//!
//! **Uncertainty is never redispatched.** Every failure after T-dispatch
//! committed (signing, adapter refusal, adapter transaction failure at any
//! stage, observe failure, a crash) leaves the receipt `dispatched`; a retry of
//! the same request then answers `RECONCILIATION_REQUIRED` at T-reserve and
//! starts no second dispatch. The broker guarantees **at most one dispatch per
//! (tenant, idempotency key)** and, through the adapter's consumed permit ids,
//! at most one effect per permit. It does **not** claim exactly-once execution
//! for external systems.
//!
//! **Partial progress (D-026, D-035 item 4).** [`Execution::evidence`] records,
//! for each lifecycle transaction that ran, whether it committed and, if it
//! failed, the stage (begin, body, commit), the sanitized store error and
//! whether an issued commit has an unknown outcome. [`Evidence::progress`] is
//! the crash state the definitely committed transactions imply;
//! [`Evidence::possible_progress`] adds a transaction whose commit outcome is
//! unknown (an ambiguous commit is never assumed to have committed nothing);
//! [`Evidence::reconciliation_required`] is `true` whenever an effect or a
//! permit consumption may have committed without an observed receipt. A
//! storage failure of the adapter is `LEDGER_UNAVAILABLE` even when a permit
//! check would also refuse. [`Evidence::diagnostics`] gives operator lines
//! with the transaction, stage, category and code only (S-5: no path, key,
//! seal or SQL).
//!
//! **Present permit** ([`Broker::present_permit`], D-035 item 3): never
//! applies an effect. Every permit is issued after its receipt became
//! `dispatched`, so presenting one outside [`Broker::execute`] could only
//! start a second effect path that bypasses the receipt lifecycle. The permit
//! is verified in a read-only adapter transaction under a shared hold of the
//! gate (storage failure first, then the D-025 B permit checks); a consumed
//! permit is `PERMIT_REPLAYED`, an unconsumed one `RECONCILIATION_REQUIRED`
//! (recovery goes through the broker).
//!
//! **Reconcile** ([`Broker::reconcile`], D-035 item 3): takes the gate
//! exclusively (D-029), so it waits for every in-flight execution and permit
//! presentation of this process and never changes one. For every
//! `dispatched` or `unknown_reconcile` receipt with a recorded permit number,
//! the adapter's durable evidence for that permit id is read; evidence bound
//! to exactly that receipt (tenant, key, fingerprint) records the observed
//! outcome by a checked transition with a `reconcile` event and a
//! reconciliation record. Without evidence a `dispatched` receipt becomes
//! `unknown_reconcile` and stays uncertain; nothing is ever redispatched, and
//! client assertions are never evidence. Evidence bound to another receipt is
//! an integrity failure (`LEDGER_UNAVAILABLE`). Repeating reconciliation
//! changes nothing further. Process-local only: a reconciler in another
//! process is refused by the single-owner ledger (M3-01).
//!
//! **Runner routing** (M3-07): a broker built with [`Broker::with_runner`]
//! sends requests of kind [`RUNNER_EFFECT_KIND`] to the runner adapter (in
//! T-adapter and `present_permit`) and every other kind to the controlled
//! effect adapter; reconciliation reads the evidence of both (a permit id
//! with a record in both is an integrity failure). Without a runner, a runner
//! request is `REQUEST_SCOPE_MISMATCH` before anything is reserved. Cost: a
//! runner job runs inside T-adapter, so the shared policy gate and the
//! workflow lock are held for the job's whole wall time (bounded by its
//! `Limits`); administration, policy activation and reconciliation (the
//! exclusive gate) and promotions of that workflow wait for it, which is what
//! keeps the attested state from changing during the run (D-036). A failure
//! of either store makes reconciliation fail closed for every receipt, since
//! the evidence of both is read.
//!
//! **Restart** (D-020): all of the above state is in the ledger and the effect
//! store; a new broker over the reopened ledger resumes a `reserved` receipt
//! (revalidating its grant) and never dispatches a `dispatched` or
//! `unknown_reconcile` one again.
//!
//! **Production differences from M1** (frozen M1 expectations unchanged):
//! - The effect store is separate from the ledger and reached only through
//!   the adapter (M1 kept consumed permits and effects in the ledger file).
//! - Permit verification adds `TIME_UNAVAILABLE` and `PERMIT_STALE` (D-033).
//! - A permit is returned to the caller in [`Execution::permit`] whenever one
//!   was issued; `present_permit` never applies an effect (M1 applied an
//!   unconsumed permit; no M1 fixture presents an unconsumed permit), and the
//!   M1 `apply_without_permit` operation has no counterpart.
//! - A resumed reservation is revalidated before dispatch (M1 resumed without
//!   rechecking the grant); a grant-backed permit expires no later than its
//!   grant (D-035 item 1).
//! - Reconciliation records an observed outcome when the adapter holds
//!   evidence for the receipt's permit (M1 marked every `dispatched` receipt
//!   `unknown_reconcile`).
//! - There is no reservation expiry or refund (D-035 item 1): an abandoned
//!   `reserved` receipt keeps its key and grant use until it is resumed;
//!   explicit cancellation or recovery is designed separately.

use crate::authorize::{authorize, Authorized};
use crate::clock::{observe, ClockError, EffectiveClock, TrustedClock};
use crate::effects::{EffectAdapter, EffectEvidence, EffectResult, VerifiedEffect};
use crate::grants::{
    GrantLabel, GrantService, GrantUse, KeyLocks, ReceiptStatus, Replay, ReserveOutcome,
    ReserveRequest, Revalidation, StoredGrant,
};
use crate::ingress::VerifiedRequest;
use crate::keys::PermitKey;
use crate::permit::{Permit, PermitId, PermitIssueError};
use crate::policy::{ActivePolicy, SharedPolicy};
use crate::refusal::Refusal;
use crate::runner::RUNNER_EFFECT_KIND;
use crate::store::{ExecuteLedger, LedgerStore, StoreError, TxFailure};
use writ_kernel::contracts::StateRevision;
use writ_kernel::json::Value;
use writ_kernel::time::Timestamp;

/// `true` when `request` is a runner evaluation ([`RUNNER_EFFECT_KIND`]),
/// which only the runner adapter may apply.
fn is_runner(request: &Value) -> bool {
    request
        .get("action")
        .and_then(|a| a.get("kind"))
        .and_then(Value::as_str)
        == Some(RUNNER_EFFECT_KIND)
}

/// The permit id of permit number `n` (`permit-<n>`).
pub fn permit_id_for(n: u64) -> Option<PermitId> {
    PermitId::new(&format!("permit-{n}"))
}

/// One receipt: tenant, idempotency key and the fingerprint it binds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReceiptRef<'a> {
    tenant_id: &'a str,
    idempotency_key: &'a str,
    fingerprint: &'a str,
}

impl ReceiptRef<'_> {
    /// Tenant of the receipt.
    pub fn tenant_id(&self) -> &str {
        self.tenant_id
    }

    /// Idempotency key of the receipt.
    pub fn idempotency_key(&self) -> &str {
        self.idempotency_key
    }

    /// The fingerprint the receipt must bind.
    pub fn fingerprint(&self) -> &str {
        self.fingerprint
    }
}

/// The reservation to dispatch: built only by the broker from a
/// [`ReserveRequest`] of a guard-bound [`Authorized`] (it borrows it, so it
/// cannot outlive the policy hold). Carries the effective grant to
/// revalidate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DispatchIntent<'a> {
    receipt: ReceiptRef<'a>,
    grant: Option<&'a GrantUse>,
}

impl<'a> DispatchIntent<'a> {
    pub(crate) fn new(request: &'a ReserveRequest<'_>) -> DispatchIntent<'a> {
        DispatchIntent {
            receipt: ReceiptRef {
                tenant_id: request.tenant_id(),
                idempotency_key: request.idempotency_key(),
                fingerprint: request.fingerprint(),
            },
            grant: request.grant(),
        }
    }

    /// Tenant of the receipt.
    pub fn tenant_id(&self) -> &str {
        self.receipt.tenant_id
    }

    /// Idempotency key of the receipt.
    pub fn idempotency_key(&self) -> &str {
        self.receipt.idempotency_key
    }

    /// The fingerprint the receipt must bind.
    pub fn fingerprint(&self) -> &str {
        self.receipt.fingerprint
    }

    /// The effective grant label the receipt must bind; `None` for `allow`.
    pub fn grant_label(&self) -> Option<&str> {
        self.grant.map(GrantUse::label)
    }

    /// Revalidate the reservation's grant before dispatch (D-035 item 1):
    /// `stored` is the grant row with its provenance, `use_recorded` whether
    /// the grant use of this receipt is recorded. Order: integrity, provenance,
    /// `GRANT_MISMATCH` (binding of the current use-time decision),
    /// `GRANT_EXPIRED` (the operation's observed time). Without an effective
    /// grant there is nothing to revalidate: integrity failure (fail closed).
    pub fn revalidate(&self, stored: Option<&StoredGrant>, use_recorded: bool) -> Revalidation {
        match self.grant {
            Some(grant) => grant.revalidate(stored, use_recorded),
            None => Revalidation::Integrity,
        }
    }
}

/// What T-dispatch committed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Dispatched {
    /// The permit number (the new value of `next_permit`).
    pub permit_number: u64,
    /// The revalidated grant's expiry; `None` for an `allow` decision.
    pub grant_expires_at: Option<Timestamp>,
}

/// The observed outcome to record: built only by the broker from a
/// guard-bound [`Authorized`] and the adapter's result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation<'a> {
    receipt: ReceiptRef<'a>,
    status: ReceiptStatus,
}

impl<'a> Observation<'a> {
    pub(crate) fn new(authorized: &'a Authorized<'_>, result: EffectResult) -> Observation<'a> {
        let request = authorized.request();
        Observation {
            receipt: ReceiptRef {
                tenant_id: &request.tenant_id,
                idempotency_key: &request.idempotency_key,
                fingerprint: authorized.verified().fingerprint().as_str(),
            },
            status: observed_status(result),
        }
    }

    /// The receipt (tenant, key, fingerprint).
    pub fn receipt(&self) -> &ReceiptRef<'a> {
        &self.receipt
    }

    /// `observed_success` or `observed_failure`.
    pub fn status(&self) -> ReceiptStatus {
        self.status
    }
}

fn observed_status(result: EffectResult) -> ReceiptStatus {
    match result {
        EffectResult::Applied => ReceiptStatus::ObservedSuccess,
        EffectResult::Failed => ReceiptStatus::ObservedFailure,
    }
}

/// A receipt whose outcome is uncertain (`dispatched` or
/// `unknown_reconcile`), as read for reconciliation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingReceipt {
    /// Tenant.
    pub tenant_id: String,
    /// Idempotency key.
    pub idempotency_key: String,
    /// Stored request fingerprint.
    pub fingerprint: String,
    /// Stored status (`dispatched` or `unknown_reconcile`).
    pub status: ReceiptStatus,
    /// The permit number recorded at dispatch; `None` when none is recorded
    /// (for example a receipt dispatched under `writ-ledger-3`).
    pub permit_number: Option<u64>,
}

/// What reconciliation decided for one pending receipt. Built only by the
/// broker from the adapter's evidence; a store cannot fabricate one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolution {
    receipt: PendingReceipt,
    observed: Option<ReceiptStatus>,
}

impl Resolution {
    /// The pending receipt.
    pub fn receipt(&self) -> &PendingReceipt {
        &self.receipt
    }

    /// `observed_success` or `observed_failure` when the adapter holds
    /// evidence for exactly this receipt's permit; `None`: uncertainty is
    /// retained.
    pub fn observed(&self) -> Option<ReceiptStatus> {
        self.observed
    }
}

/// What one reconciliation changed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Reconciled {
    /// Receipts recorded `observed_success` from adapter evidence.
    pub observed_success: u64,
    /// Receipts recorded `observed_failure` from adapter evidence.
    pub observed_failure: u64,
    /// `dispatched` receipts marked `unknown_reconcile` (no evidence).
    pub unknown: u64,
}

impl Reconciled {
    /// Number of receipts changed.
    pub fn changed(&self) -> u64 {
        self.observed_success + self.observed_failure + self.unknown
    }
}

/// The logical transactions of the execute lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TxName {
    /// T-reserve.
    Reserve,
    /// T-dispatch.
    Dispatch,
    /// T-adapter (the effect adapter's own transaction).
    Adapter,
    /// T-observe.
    Observe,
}

impl TxName {
    /// `reserve`, `dispatch`, `adapter` or `observe`.
    pub fn as_str(self) -> &'static str {
        match self {
            TxName::Reserve => "reserve",
            TxName::Dispatch => "dispatch",
            TxName::Adapter => "adapter",
            TxName::Observe => "observe",
        }
    }

    fn implies(self) -> Progress {
        match self {
            TxName::Reserve => Progress::Reserved,
            TxName::Dispatch => Progress::DispatchedWithoutEffect,
            TxName::Adapter => Progress::DispatchedWithEffect,
            TxName::Observe => Progress::Observed,
        }
    }
}

/// One lifecycle transaction that ran.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TxRecord {
    /// Which transaction.
    pub name: TxName,
    /// `true` when it committed (including the write-free commit of a resume).
    pub committed: bool,
    /// The failure, with its stage, when it failed (then `committed` is false).
    pub failure: Option<TxFailure>,
}

/// The crash state committed transactions of one execution imply (D-026
/// category 4 modelling), in lifecycle order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Progress {
    /// No lifecycle transaction committed (refused, replayed, or T-reserve failed).
    NoChange,
    /// T-reserve committed, T-dispatch did not: the receipt is `reserved`
    /// (grant use kept); a retry resumes.
    Reserved,
    /// T-dispatch committed, no adapter transaction did: `dispatched`, no
    /// consumed permit, no effect (uncertain to the caller).
    DispatchedWithoutEffect,
    /// T-adapter committed, T-observe did not: `dispatched` with the consumed
    /// permit and the effect (or its recorded failure) (uncertain to the caller).
    DispatchedWithEffect,
    /// T-observe committed: `observed_success` or `observed_failure`.
    Observed,
}

impl Progress {
    /// `no_change`, `reserved`, `dispatched_without_effect`,
    /// `dispatched_with_effect` or `observed`.
    pub fn as_str(self) -> &'static str {
        match self {
            Progress::NoChange => "no_change",
            Progress::Reserved => "reserved",
            Progress::DispatchedWithoutEffect => "dispatched_without_effect",
            Progress::DispatchedWithEffect => "dispatched_with_effect",
            Progress::Observed => "observed",
        }
    }
}

/// Transaction evidence of one execution (D-026).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Evidence {
    /// The lifecycle transactions in the order they ran.
    pub transactions: Vec<TxRecord>,
}

impl Evidence {
    fn done(&mut self, name: TxName, committed: bool) {
        self.transactions.push(TxRecord {
            name,
            committed,
            failure: None,
        });
    }

    fn failed(&mut self, name: TxName, failure: &TxFailure) -> Refusal {
        self.transactions.push(TxRecord {
            name,
            committed: false,
            failure: Some(failure.clone()),
        });
        Refusal::LedgerUnavailable(failure.error.clone())
    }

    /// The transaction that failed and its failure, if any.
    pub fn failure(&self) -> Option<(TxName, &TxFailure)> {
        self.transactions
            .iter()
            .find_map(|t| t.failure.as_ref().map(|f| (t.name, f)))
    }

    /// The crash state implied by the definitely committed transactions.
    pub fn progress(&self) -> Progress {
        self.transactions
            .iter()
            .rev()
            .find(|t| t.committed)
            .map_or(Progress::NoChange, |t| t.name.implies())
    }

    /// The furthest crash state that may be durable: [`Evidence::progress`],
    /// or the state implied by a transaction whose issued commit has an
    /// unknown outcome (D-035 item 4).
    pub fn possible_progress(&self) -> Progress {
        let unknown = self
            .failure()
            .filter(|(_, f)| f.outcome_unknown)
            .map(|(name, _)| name.implies());
        unknown.map_or(self.progress(), |p| p.max(self.progress()))
    }

    /// `true` when the receipt may be `dispatched` without an observed
    /// outcome: a dispatch, a permit consumption or an effect may have
    /// committed, so only reconciliation may proceed.
    pub fn reconciliation_required(&self) -> bool {
        self.progress() != Progress::Observed
            && self.possible_progress() >= Progress::DispatchedWithoutEffect
    }

    /// The failure record to audit with the refusal, when a lifecycle
    /// transaction failed.
    pub fn failure_record(&self) -> Option<FailureRecord> {
        let (name, failure) = self.failure()?;
        let (category, code) = match &failure.error {
            StoreError::Unavailable { category, code } => (category.clone(), code.clone()),
            other => ("ownership".to_string(), other.code().to_string()),
        };
        Some(FailureRecord {
            transaction: name,
            stage: failure.stage.as_str(),
            category,
            code,
            progress: self.progress(),
            possible_progress: self.possible_progress(),
            outcome_unknown: failure.outcome_unknown,
        })
    }

    /// Sanitized operator diagnostics, one line per failed transaction:
    /// `<transaction> <stage> <category> <code>`, plus `outcome_unknown` when
    /// an issued commit has an unknown outcome (S-5, D-025: no path, file
    /// name, key, seal or SQL text).
    pub fn diagnostics(&self) -> Vec<String> {
        self.transactions
            .iter()
            .filter_map(|t| {
                t.failure.as_ref().map(|f| {
                    let line = format!("{} {} {}", t.name.as_str(), f.stage.as_str(), f.error);
                    if f.outcome_unknown {
                        format!("{line} outcome_unknown")
                    } else {
                        line
                    }
                })
            })
            .collect()
    }
}

/// The audited record of a refusal caused by a failed lifecycle transaction
/// (D-035 item 4): which transaction failed and where, the sanitized
/// category and code, the durable progress and the furthest progress that
/// may be durable, and whether an issued commit has an unknown outcome.
/// Built only by the broker from [`Evidence`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FailureRecord {
    transaction: TxName,
    stage: &'static str,
    category: String,
    code: String,
    progress: Progress,
    possible_progress: Progress,
    outcome_unknown: bool,
}

impl FailureRecord {
    /// The failed transaction.
    pub fn transaction(&self) -> &'static str {
        self.transaction.as_str()
    }

    /// `begin`, `body` or `commit`.
    pub fn stage(&self) -> &'static str {
        self.stage
    }

    /// Sanitized operator category.
    pub fn category(&self) -> &str {
        &self.category
    }

    /// Sanitized code.
    pub fn code(&self) -> &str {
        &self.code
    }

    /// The definitely durable progress.
    pub fn progress(&self) -> &'static str {
        self.progress.as_str()
    }

    /// The furthest progress that may be durable.
    pub fn possible_progress(&self) -> &'static str {
        self.possible_progress.as_str()
    }

    /// `true` when an issued commit has an unknown outcome.
    pub fn outcome_unknown(&self) -> bool {
        self.outcome_unknown
    }
}

/// What a successful execution did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Executed {
    /// `{"result": "effect_completed"}`: the receipt is `observed_success`.
    EffectCompleted,
    /// `{"result": "effect_failed"}`: the receipt is `observed_failure`.
    EffectFailed,
    /// An idempotent retry of an observed receipt; nothing changed.
    Replayed(Replay),
}

/// The answer of [`Broker::execute`] with its evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Execution {
    /// The result (a refusal is already audited).
    pub result: Result<Executed, Refusal>,
    /// The permit, whenever T-dispatch committed and one was issued.
    pub permit: Option<Permit>,
    /// The lifecycle transactions that ran.
    pub evidence: Evidence,
}

/// A [`TrustedClock`] that persists every reading as the ledger's floor
/// before returning it ([`observe`]); used for the permit expiry check
/// inside the adapter transaction.
struct FloorClock<'a, L: ?Sized> {
    clock: &'a (dyn EffectiveClock + Sync),
    ledger: &'a L,
}

impl<L: ?Sized> TrustedClock for FloorClock<'_, L>
where
    for<'b> &'b L: LedgerStore,
{
    fn now(&self) -> Result<Timestamp, ClockError> {
        let mut ledger: &L = self.ledger;
        observe(self.clock, &mut ledger).map(|o| o.at())
    }
}

/// The broker's execute lifecycle over one shared ledger and one effect
/// adapter (module documentation). Shared by every thread of the owning
/// process; it owns the process's per-key locks (through its
/// [`GrantService`]).
pub struct Broker<'a, L: ?Sized, A: ?Sized> {
    grants: GrantService<'a, L>,
    adapter: &'a A,
    /// The runner adapter for [`RUNNER_EFFECT_KIND`] requests (M3-07), if any.
    runner: Option<&'a dyn EffectAdapter>,
    permit_key: &'a PermitKey,
    /// Per-workflow locks (M3-05): a promotion, and an execution whose
    /// request names a workflow, hold their workflow's lock from validation
    /// to commit (effect prerequisites, D-036). Taken after the gate and
    /// before the per-key lock.
    pub(crate) workflow_locks: KeyLocks,
}

impl<'a, L, A> Broker<'a, L, A>
where
    L: ExecuteLedger + ?Sized,
    for<'b> &'b L: LedgerStore,
    A: EffectAdapter + ?Sized,
{
    /// A broker over `ledger` and `adapter`, deciding under `active` with time
    /// from `clock`, signing permits with `permit_key`.
    pub fn new(
        active: &'a ActivePolicy,
        clock: &'a (dyn EffectiveClock + Sync),
        ledger: &'a L,
        adapter: &'a A,
        permit_key: &'a PermitKey,
    ) -> Broker<'a, L, A> {
        Broker {
            grants: GrantService::new(active, clock, ledger),
            adapter,
            runner: None,
            permit_key,
            workflow_locks: KeyLocks::new(),
        }
    }

    /// The same broker with `runner` as the adapter of [`RUNNER_EFFECT_KIND`]
    /// requests (M3-07). Requests of every other kind still go to the
    /// controlled effect adapter; without a runner, a runner request is
    /// refused before anything is reserved (`REQUEST_SCOPE_MISMATCH`).
    pub fn with_runner(mut self, runner: &'a dyn EffectAdapter) -> Broker<'a, L, A> {
        self.runner = Some(runner);
        self
    }

    /// Approval issuance and the per-key locks (shared with execution).
    pub fn grants(&self) -> &GrantService<'a, L> {
        &self.grants
    }

    /// Execute `verified` (module documentation for the order).
    pub fn execute(
        &self,
        verified: &VerifiedRequest,
        state_revision: StateRevision,
        grant: Option<&GrantLabel>,
    ) -> Execution {
        let mut evidence = Evidence::default();
        let mut permit = None;
        let result = self.execute_held(verified, state_revision, grant, &mut evidence, &mut permit);
        let result = match (result, evidence.failure_record()) {
            (Err(refusal), Some(record)) => {
                // Audited with its failure record; an audit failure does not
                // change the answer (storage rule 2).
                let _ = self.grants.ledger.record_failure(refusal.code(), &record);
                Err(refusal)
            }
            (result, _) => self.grants.audited(result),
        };
        Execution {
            result,
            permit,
            evidence,
        }
    }

    /// The execution under the shared hold and the per-key lock; both are
    /// released when this returns, before the refusal audit.
    fn execute_held(
        &self,
        verified: &VerifiedRequest,
        state_revision: StateRevision,
        grant: Option<&GrantLabel>,
        evidence: &mut Evidence,
        permit: &mut Option<Permit>,
    ) -> Result<Executed, Refusal> {
        let shared = self.grants.active.shared();
        let observed = self.grants.observe()?;
        let inputs = shared.trusted_inputs(state_revision);
        let authorized = authorize(verified, &inputs, &observed)?;
        let request = ReserveRequest::new(&authorized, observed, grant)?;
        if is_runner(verified.request()) && self.runner.is_none() {
            return Err(Refusal::RequestScopeMismatch);
        }
        // A request naming a workflow holds the workflow lock from its
        // prerequisite check in T-reserve until the effect is observed, so no
        // promotion can change the workflow in between (D-036); administrative
        // changes wait for the shared gate.
        let effect = request.effect();
        let _workflow = effect.workflow_id.map(|w| {
            self.workflow_locks
                .lock(request.tenant_id(), &format!("{}/{w}", effect.project_id))
        });
        let _key = self
            .grants
            .key_locks
            .lock(request.tenant_id(), request.idempotency_key());
        let ledger = self.grants.ledger;
        match ledger.reserve(&request) {
            Err(f) => return Err(evidence.failed(TxName::Reserve, &f)),
            Ok(Err(refusal)) => {
                evidence.done(TxName::Reserve, false);
                return Err(refusal);
            }
            Ok(Ok(ReserveOutcome::Replayed(replay))) => {
                evidence.done(TxName::Reserve, false);
                return Ok(Executed::Replayed(replay));
            }
            Ok(Ok(ReserveOutcome::Reserved | ReserveOutcome::Resumed)) => {
                evidence.done(TxName::Reserve, true);
            }
        }
        let dispatched = match ledger.dispatch(&DispatchIntent::new(&request)) {
            Err(f) => return Err(evidence.failed(TxName::Dispatch, &f)),
            Ok(Err(refusal)) => {
                // Revalidation refused: the receipt stays `reserved` with its
                // grant use (D-035 item 1).
                evidence.done(TxName::Dispatch, false);
                return Err(refusal);
            }
            Ok(Ok(dispatched)) => {
                evidence.done(TxName::Dispatch, true);
                dispatched
            }
        };
        // From here on the receipt is `dispatched`: every failure leaves it so.
        let id = permit_id_for(dispatched.permit_number).ok_or(Refusal::PermitInvalid)?;
        let issued = Permit::issue_bounded(
            &id,
            &authorized,
            &observed,
            self.permit_key,
            dispatched.grant_expires_at,
        )
        .map_err(|e| match e {
            PermitIssueError::TimeUnavailable => Refusal::TimeUnavailable,
            PermitIssueError::Signing => Refusal::PermitInvalid,
        })?;
        *permit = Some(issued.clone());
        let result = match self.adapt(authorized.hold(), &issued, verified.request()) {
            Err(f) => return Err(evidence.failed(TxName::Adapter, &f)),
            Ok(Err(refusal)) => {
                evidence.done(TxName::Adapter, false);
                return Err(refusal);
            }
            Ok(Ok(result)) => {
                evidence.done(TxName::Adapter, true);
                result
            }
        };
        ledger
            .observe_effect(&Observation::new(&authorized, result))
            .map_err(|f| evidence.failed(TxName::Observe, &f))?;
        evidence.done(TxName::Observe, true);
        Ok(match result {
            EffectResult::Applied => Executed::EffectCompleted,
            EffectResult::Failed => Executed::EffectFailed,
        })
    }

    fn floor_clock(&self) -> FloorClock<'a, L> {
        FloorClock {
            clock: self.grants.clock,
            ledger: self.grants.ledger,
        }
    }

    /// T-adapter for `permit` and the presented `request`, verified under
    /// `hold` inside the adapter transaction.
    fn adapt(
        &self,
        hold: &SharedPolicy<'_>,
        permit: &Permit,
        request: &Value,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let clock = self.floor_clock();
        let key = self.permit_key;
        let mut verify = || {
            let verified = permit.verify(request, key, &clock, hold)?;
            VerifiedEffect::new(verified, request)
        };
        match (is_runner(request), self.runner) {
            (false, _) => self.adapter.apply(&mut verify),
            (true, Some(runner)) => runner.apply(&mut verify),
            (true, None) => Ok(Err(Refusal::RequestScopeMismatch)),
        }
    }

    /// Present a client-held `permit` with `request` (D-035 item 3; module
    /// documentation): never applies an effect. Under a shared hold of the
    /// gate, a read-only adapter transaction verifies the permit: a storage
    /// failure is `LEDGER_UNAVAILABLE` and takes precedence over every permit
    /// check (D-025 B, D-035 item 4); then the permit refusals in their
    /// order; a consumed permit with a recorded outcome is `PERMIT_REPLAYED`;
    /// an unconsumed one belongs to a `dispatched` or `unknown_reconcile`
    /// receipt and is `RECONCILIATION_REQUIRED`, as is a runner permit
    /// consumed by a run whose outcome was never recorded (no completion
    /// evidence). The answer is audited.
    pub fn present_permit(&self, permit: &Permit, request: &Value) -> Refusal {
        let refusal = {
            let shared = self.grants.active.shared();
            let clock = self.floor_clock();
            let key = self.permit_key;
            let mut verify = || {
                let verified = permit.verify(request, key, &clock, &shared)?;
                VerifiedEffect::new(verified, request)
            };
            let inspected = match (is_runner(request), self.runner) {
                (false, _) => self.adapter.inspect(&mut verify),
                (true, Some(runner)) => runner.inspect(&mut verify),
                (true, None) => Ok(Err(Refusal::RequestScopeMismatch)),
            };
            match inspected {
                Err(f) => Refusal::LedgerUnavailable(f.error),
                Ok(Err(refusal)) => refusal,
                Ok(Ok(Some(_))) => Refusal::PermitReplayed,
                Ok(Ok(None)) => Refusal::ReconciliationRequired,
            }
        };
        let _ = self.grants.audited(Err::<(), Refusal>(refusal.clone()));
        refusal
    }

    /// Reconcile under the exclusive gate (D-035 item 3; module
    /// documentation). Refusals are audited.
    pub fn reconcile(&self) -> Result<Reconciled, Refusal> {
        let result = {
            let hold = self.grants.active.exclusive();
            self.reconcile_held(&hold)
        };
        self.grants.audited(result)
    }

    fn reconcile_held(
        &self,
        hold: &crate::policy::ExclusiveHold<'_>,
    ) -> Result<Reconciled, Refusal> {
        let ledger = self.grants.ledger;
        let unavailable = |f: TxFailure| Refusal::LedgerUnavailable(f.error);
        let pending = ledger.pending_receipts(hold).map_err(unavailable)?;
        let mut resolutions = Vec::with_capacity(pending.len());
        for receipt in pending {
            let evidence = match receipt.permit_number.and_then(permit_id_for) {
                None => None,
                Some(id) => self.evidence(id.as_str()).map_err(unavailable)?,
            };
            let observed = match evidence {
                None => None,
                Some(e) => Some(Self::bound(&receipt, &e)?),
            };
            resolutions.push(Resolution { receipt, observed });
        }
        ledger.reconcile(hold, &resolutions).map_err(unavailable)
    }

    /// The evidence for `permit_id` held by the controlled effect adapter or
    /// the runner (permit ids are unique per ledger, so at most one holds a
    /// record; a record in both is an integrity failure).
    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure> {
        let controlled = self.adapter.evidence(permit_id)?;
        let runner = match self.runner {
            Some(runner) => runner.evidence(permit_id)?,
            None => None,
        };
        match (controlled, runner) {
            (Some(_), Some(_)) => Err(TxFailure::new(
                crate::store::TxStage::Body,
                StoreError::Unavailable {
                    category: "reconcile evidence".into(),
                    code: "integrity".into(),
                },
            )),
            (Some(e), None) | (None, Some(e)) => Ok(Some(e)),
            (None, None) => Ok(None),
        }
    }

    /// The observed status `evidence` establishes for `receipt`; evidence
    /// for the receipt's permit that binds another effect is an integrity
    /// failure.
    fn bound(
        receipt: &PendingReceipt,
        evidence: &EffectEvidence,
    ) -> Result<ReceiptStatus, Refusal> {
        if !evidence.binds(
            &receipt.tenant_id,
            &receipt.idempotency_key,
            &receipt.fingerprint,
        ) {
            return Err(Refusal::LedgerUnavailable(StoreError::Unavailable {
                category: "reconcile evidence".into(),
                code: "integrity".into(),
            }));
        }
        Ok(observed_status(evidence.result))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::store::{StoreError, TxStage};

    fn rec(name: TxName, committed: bool) -> TxRecord {
        TxRecord {
            name,
            committed,
            failure: None,
        }
    }

    fn fail(name: TxName, stage: TxStage) -> TxRecord {
        TxRecord {
            name,
            committed: false,
            failure: Some(TxFailure::new(
                stage,
                StoreError::Unavailable {
                    category: format!("transaction {}", name.as_str()),
                    code: "SQLITE_IOERR".into(),
                },
            )),
        }
    }

    #[test]
    fn progress_is_the_crash_state_of_the_committed_transactions() {
        use TxName::*;
        let cases = [
            (vec![], Progress::NoChange),
            (vec![fail(Reserve, TxStage::Begin)], Progress::NoChange),
            (vec![rec(Reserve, false)], Progress::NoChange),
            (
                vec![rec(Reserve, true), fail(Dispatch, TxStage::Commit)],
                Progress::Reserved,
            ),
            (
                vec![
                    rec(Reserve, true),
                    rec(Dispatch, true),
                    fail(Adapter, TxStage::Body),
                ],
                Progress::DispatchedWithoutEffect,
            ),
            (
                vec![rec(Reserve, true), rec(Dispatch, true), rec(Adapter, false)],
                Progress::DispatchedWithoutEffect,
            ),
            (
                vec![
                    rec(Reserve, true),
                    rec(Dispatch, true),
                    rec(Adapter, true),
                    fail(Observe, TxStage::Begin),
                ],
                Progress::DispatchedWithEffect,
            ),
            (
                vec![
                    rec(Reserve, true),
                    rec(Dispatch, true),
                    rec(Adapter, true),
                    rec(Observe, true),
                ],
                Progress::Observed,
            ),
        ];
        for (transactions, want) in cases {
            let e = Evidence { transactions };
            assert_eq!(e.progress(), want, "{e:?}");
        }
    }

    #[test]
    fn an_ambiguous_commit_is_never_assumed_to_have_committed_nothing() {
        use TxName::*;
        let unknown = |name: TxName| TxRecord {
            name,
            committed: false,
            failure: Some(TxFailure::commit_unknown(StoreError::Unavailable {
                category: format!("transaction {}", name.as_str()),
                code: "SQLITE_IOERR".into(),
            })),
        };
        let cases = [
            // (transactions, definite, possible, reconciliation required)
            (
                vec![unknown(Reserve)],
                Progress::NoChange,
                Progress::Reserved,
                false,
            ),
            (
                vec![rec(Reserve, true), unknown(Dispatch)],
                Progress::Reserved,
                Progress::DispatchedWithoutEffect,
                true,
            ),
            (
                vec![rec(Reserve, true), rec(Dispatch, true), unknown(Adapter)],
                Progress::DispatchedWithoutEffect,
                Progress::DispatchedWithEffect,
                true,
            ),
            (
                vec![
                    rec(Reserve, true),
                    rec(Dispatch, true),
                    rec(Adapter, true),
                    unknown(Observe),
                ],
                Progress::DispatchedWithEffect,
                Progress::Observed,
                true,
            ),
            (
                vec![
                    rec(Reserve, true),
                    rec(Dispatch, true),
                    fail(Adapter, TxStage::Commit),
                ],
                Progress::DispatchedWithoutEffect,
                Progress::DispatchedWithoutEffect,
                true,
            ),
            (
                vec![rec(Reserve, true), fail(Dispatch, TxStage::Body)],
                Progress::Reserved,
                Progress::Reserved,
                false,
            ),
        ];
        for (transactions, definite, possible, required) in cases {
            let e = Evidence { transactions };
            assert_eq!(e.progress(), definite, "{e:?}");
            assert_eq!(e.possible_progress(), possible, "{e:?}");
            assert_eq!(e.reconciliation_required(), required, "{e:?}");
            let record = e.failure_record().unwrap();
            assert_eq!(record.progress(), definite.as_str());
            assert_eq!(record.possible_progress(), possible.as_str());
        }
        let e = Evidence {
            transactions: vec![rec(Reserve, true), rec(Dispatch, true), unknown(Adapter)],
        };
        assert_eq!(
            e.diagnostics(),
            vec!["adapter commit transaction adapter SQLITE_IOERR outcome_unknown".to_string()]
        );
        let record = e.failure_record().unwrap();
        assert_eq!(
            (
                record.transaction(),
                record.stage(),
                record.code(),
                record.outcome_unknown()
            ),
            ("adapter", "commit", "SQLITE_IOERR", true)
        );
    }

    #[test]
    fn evidence_names_the_failed_transaction_stage_and_a_sanitized_code() {
        let e = Evidence {
            transactions: vec![
                rec(TxName::Reserve, true),
                rec(TxName::Dispatch, true),
                fail(TxName::Adapter, TxStage::Commit),
            ],
        };
        let (name, failure) = e.failure().unwrap();
        assert_eq!(name, TxName::Adapter);
        assert_eq!(failure.stage, TxStage::Commit);
        assert!(!failure.is_busy());
        assert_eq!(
            e.diagnostics(),
            vec!["adapter commit transaction adapter SQLITE_IOERR".to_string()]
        );
        assert_eq!(
            failure.to_string(),
            "transaction adapter SQLITE_IOERR at commit"
        );
    }
}
