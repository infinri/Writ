//! The execute lifecycle after T-reserve (M3-04; D-008, D-012, D-020, D-025
//! B and C-3, D-026, D-029, D-033 item 2, D-034 policy-gate ownership;
//! m1/fixtures/broker/BROKER.md `execute`, `present_permit`, `reconcile`,
//! `restart_broker`; LEDGER-SQLITE.md T-dispatch, T-adapter, T-observe,
//! reconcile and storage failure rules).
//!
//! **Execute** ([`Broker::execute`]), in order, under one shared hold of the
//! policy gate for the whole operation (D-029 lock order: gate, per-key lock,
//! then each transaction):
//!
//! 1. Trusted time is observed (floor persisted, `TIME_UNAVAILABLE`), the
//!    request is re-decided at use time; `require_approval` without a grant is
//!    `APPROVAL_REQUIRED`. Then the per-key lock is taken and held until the
//!    operation returns.
//! 2. **T-reserve** ([`crate::store::GrantLedger::reserve`]): refusal, replay,
//!    `RECONCILIATION_REQUIRED` for a `dispatched` or `unknown_reconcile`
//!    receipt, resume of a `reserved` one, or a new reservation.
//! 3. **T-dispatch** ([`ExecuteLedger::dispatch`]): `next_permit` incremented,
//!    the receipt `reserved` to `dispatched` (exactly one row), a `dispatch`
//!    event; committed before any effect can start. The permit `permit-<n>`
//!    is then issued from the guard-bound authorization, expiring at
//!    `min(now + 30 s, deadline)` with `now` the operation's observed time.
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
//! are released (audit failure does not change the answer).
//!
//! **Uncertainty is never redispatched.** Every failure after T-dispatch
//! committed (signing, adapter refusal, adapter transaction failure at any
//! stage, observe failure, a crash) leaves the receipt `dispatched`; a retry of
//! the same request then answers `RECONCILIATION_REQUIRED` at T-reserve and
//! starts no second dispatch. The broker guarantees **at most one dispatch per
//! (tenant, idempotency key)** and, through the adapter's consumed permit ids,
//! at most one effect per permit. It does **not** claim exactly-once execution
//! for external systems: an effect whose outcome is uncertain stays uncertain
//! until reconciliation, which never creates a receipt or marks an effect
//! completed.
//!
//! **Partial progress (D-026).** [`Execution::evidence`] records, for each
//! lifecycle transaction that ran, whether it committed and, if it failed, the
//! stage (begin, body, commit) and the sanitized store error.
//! [`Evidence::progress`] is the crash state the committed transactions imply:
//! dispatch failed: `reserved` (grant use kept, a retry resumes); adapter
//! failed: `dispatched` without consumed permit or effect; observe failed:
//! `dispatched` with the consumed permit and the effect (or the recorded
//! failure). [`Evidence::diagnostics`] gives operator lines with the
//! transaction, stage, category and code only (S-5: no path, key, seal or SQL).
//!
//! **Present permit** ([`Broker::present_permit`]): a client-held permit and a
//! request go through T-adapter under a shared hold of the gate; the receipt is
//! not touched (BROKER.md `present_permit`).
//!
//! **Reconcile** ([`Broker::reconcile`]): takes the gate exclusively (D-029), so
//! it waits for every in-flight execution and permit presentation of this
//! process and never changes one; then every `dispatched` receipt becomes
//! `unknown_reconcile`. Process-local only: a reconciler in another process is
//! refused by the single-owner ledger (M3-01), not coordinated by this gate.
//!
//! **Restart** (D-020): all of the above state is in the ledger and the effect
//! store; a new broker over the reopened ledger resumes a `reserved` receipt and
//! never dispatches a `dispatched` or `unknown_reconcile` one again.
//!
//! **Open question, not implemented (C-3, reservation lease or expiry):** a
//! `reserved` receipt whose execution was abandoned (crash after T-reserve,
//! dispatch failure) keeps its key and, with approval, its consumed grant use
//! until the same request is retried, which resumes it. Nothing expires or
//! releases it. Whether a reservation should carry a lease or expiry, what
//! happens to its grant use when it lapses (returned, or kept consumed), how a
//! lapse interacts with an in-flight retry holding the key lock and with a
//! permit already issued, and which trusted time bounds it, are new semantics
//! that need an owner decision; until then an abandoned reservation can block
//! its key indefinitely (fail closed: nothing is dispatched).
//!
//! **Production differences from M1** (frozen M1 expectations unchanged):
//! - The effect store is separate from the ledger and reached only through
//!   the adapter (M1 kept consumed permits and effects in the ledger file).
//! - Permit verification adds `TIME_UNAVAILABLE` and `PERMIT_STALE` (D-033).
//! - A permit is returned to the caller in [`Execution::permit`] whenever one
//!   was issued (M1 kept issued permits in scenario state for
//!   `present_permit`); the M1 `apply_without_permit` operation has no
//!   counterpart (there is no API that applies an effect without a permit).

use crate::authorize::{authorize, Authorized};
use crate::clock::{observe, ClockError, EffectiveClock, TrustedClock};
use crate::effects::{EffectAdapter, EffectResult, VerifiedEffect};
use crate::grants::{
    GrantLabel, GrantService, ReceiptStatus, Replay, ReserveOutcome, ReserveRequest,
};
use crate::ingress::VerifiedRequest;
use crate::keys::PermitKey;
use crate::permit::{Permit, PermitId, PermitIssueError};
use crate::policy::{ActivePolicy, SharedPolicy};
use crate::refusal::Refusal;
use crate::store::{ExecuteLedger, LedgerStore, TxFailure};
use writ_kernel::contracts::StateRevision;
use writ_kernel::json::Value;
use writ_kernel::time::Timestamp;

/// The receipt to dispatch: built only by the broker from a guard-bound
/// [`Authorized`] (it borrows it, so it cannot outlive the policy hold).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DispatchIntent<'a> {
    tenant_id: &'a str,
    idempotency_key: &'a str,
    fingerprint: &'a str,
}

impl<'a> DispatchIntent<'a> {
    pub(crate) fn new(authorized: &'a Authorized<'_>) -> DispatchIntent<'a> {
        let request = authorized.request();
        DispatchIntent {
            tenant_id: &request.tenant_id,
            idempotency_key: &request.idempotency_key,
            fingerprint: authorized.verified().fingerprint().as_str(),
        }
    }

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

/// The observed outcome to record: built only by the broker from a
/// guard-bound [`Authorized`] and the adapter's result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation<'a> {
    intent: DispatchIntent<'a>,
    status: ReceiptStatus,
}

impl<'a> Observation<'a> {
    pub(crate) fn new(authorized: &'a Authorized<'_>, result: EffectResult) -> Observation<'a> {
        Observation {
            intent: DispatchIntent::new(authorized),
            status: match result {
                EffectResult::Applied => ReceiptStatus::ObservedSuccess,
                EffectResult::Failed => ReceiptStatus::ObservedFailure,
            },
        }
    }

    /// The receipt (tenant, key, fingerprint).
    pub fn intent(&self) -> &DispatchIntent<'a> {
        &self.intent
    }

    /// `observed_success` or `observed_failure`.
    pub fn status(&self) -> ReceiptStatus {
        self.status
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

/// The crash state the committed transactions of one execution imply (D-026
/// category 4 modelling).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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

    /// The crash state implied by the committed transactions.
    pub fn progress(&self) -> Progress {
        let last = self
            .transactions
            .iter()
            .rev()
            .find(|t| t.committed)
            .map(|t| t.name);
        match last {
            None => Progress::NoChange,
            Some(TxName::Reserve) => Progress::Reserved,
            Some(TxName::Dispatch) => Progress::DispatchedWithoutEffect,
            Some(TxName::Adapter) => Progress::DispatchedWithEffect,
            Some(TxName::Observe) => Progress::Observed,
        }
    }

    /// Sanitized operator diagnostics, one line per failed transaction:
    /// `<transaction> <stage> <category> <code>` (S-5, D-025: no path, file
    /// name, key, seal or SQL text).
    pub fn diagnostics(&self) -> Vec<String> {
        self.transactions
            .iter()
            .filter_map(|t| {
                t.failure
                    .as_ref()
                    .map(|f| format!("{} {} {}", t.name.as_str(), f.stage.as_str(), f.error))
            })
            .collect()
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
    /// The permit, whenever T-dispatch committed and one was issued (the
    /// client-held permit of BROKER.md `present_permit`).
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
    permit_key: &'a PermitKey,
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
            permit_key,
        }
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
        Execution {
            result: self.grants.audited(result),
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
        let number = ledger
            .dispatch(&DispatchIntent::new(&authorized))
            .map_err(|f| evidence.failed(TxName::Dispatch, &f))?;
        evidence.done(TxName::Dispatch, true);
        // From here on the receipt is `dispatched`: every failure leaves it so.
        let id = PermitId::new(&format!("permit-{number}")).ok_or(Refusal::PermitInvalid)?;
        let issued =
            Permit::issue(&id, &authorized, &observed, self.permit_key).map_err(|e| match e {
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

    /// T-adapter for `permit` and the presented `request`, verified under
    /// `hold` inside the adapter transaction.
    fn adapt(
        &self,
        hold: &SharedPolicy<'_>,
        permit: &Permit,
        request: &Value,
    ) -> Result<Result<EffectResult, Refusal>, TxFailure> {
        let clock = FloorClock {
            clock: self.grants.clock,
            ledger: self.grants.ledger,
        };
        let key = self.permit_key;
        self.adapter.apply(&mut || {
            let verified = permit.verify(request, key, &clock, hold)?;
            VerifiedEffect::new(verified, request)
        })
    }

    /// Present a client-held `permit` with `request` to the effect adapter
    /// (BROKER.md `present_permit`) under a shared hold of the gate. A store
    /// failure of the adapter transaction is `LEDGER_UNAVAILABLE` and takes
    /// precedence over any permit check (D-025 B). Refusals are audited.
    pub fn present_permit(
        &self,
        permit: &Permit,
        request: &Value,
    ) -> Result<EffectResult, Refusal> {
        let result = {
            let shared = self.grants.active.shared();
            match self.adapt(&shared, permit, request) {
                Ok(answer) => answer,
                Err(f) => Err(Refusal::LedgerUnavailable(f.error)),
            }
        };
        self.grants.audited(result)
    }

    /// Reconcile under the exclusive gate: every `dispatched` receipt becomes
    /// `unknown_reconcile`; returns how many. Refusals are audited.
    pub fn reconcile(&self) -> Result<u64, Refusal> {
        let result = {
            let hold = self.grants.active.exclusive();
            self.grants
                .ledger
                .reconcile(&hold)
                .map_err(|f| Refusal::LedgerUnavailable(f.error))
        };
        self.grants.audited(result)
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
