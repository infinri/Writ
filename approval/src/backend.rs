//! What the approval page needs from the broker (M4-11; D-040 O-2, O-3, O-11; I-2).
//!
//! The listener sees the ledger only through [`Backend`]: a read of a stored approval
//! request (its fingerprint-covered context, R6) and the two ceremony transactions. The
//! operation, its target and the idempotency key are the listener's server-side state;
//! the verifier, its relying party and the channel identity are the ledger's configured
//! [`writ_broker::human::ApprovalChannel`] (O-2, M1), never a call argument.
//! [`LedgerBackend`] adapts any [`ApprovalLedger`] with a trusted clock.

use std::sync::Arc;

use writ_broker::approval::{ApprovalRequest, ApprovalStatus};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyOutcome, IssuedChallenge,
};
use writ_broker::clock::{observe, EffectiveClock, ObservedTime};
use writ_broker::human::SubjectId;
use writ_broker::refusal::Refusal;
use writ_broker::store::LedgerStore;

/// The ledger or the trusted clock could not answer (fail closed).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Unavailable;

/// The broker operations the approval page uses.
pub trait Backend: Send + Sync {
    /// A stored approval request and its status (a read).
    fn request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, Unavailable>;

    /// Issue a challenge for `op` by `subject` (the ledger's `begin_ceremony`).
    fn begin(
        &self,
        op: &CeremonyOp,
        subject: &SubjectId,
        idempotency_key: &str,
    ) -> Result<Result<IssuedChallenge, Refusal>, Unavailable>;

    /// Finish the challenge with the client's assertion (the ledger's `finish_ceremony`).
    fn finish(
        &self,
        op: &CeremonyOp,
        challenge_id: &str,
        response: &[u8],
        idempotency_key: &str,
    ) -> Result<Result<CeremonyOutcome, Refusal>, Unavailable>;
}

/// [`Backend`] over an [`ApprovalLedger`] (opened with its approval channel configured)
/// and its trusted clock.
pub struct LedgerBackend<L, C> {
    ledger: Arc<L>,
    clock: Arc<C>,
}

impl<L, C> LedgerBackend<L, C> {
    /// The adapter.
    pub fn new(ledger: Arc<L>, clock: Arc<C>) -> LedgerBackend<L, C> {
        LedgerBackend { ledger, clock }
    }
}

impl<L, C> LedgerBackend<L, C>
where
    L: ApprovalLedger,
    for<'a> &'a L: LedgerStore,
    C: EffectiveClock,
{
    fn now(&self) -> Result<ObservedTime, Unavailable> {
        let mut store: &L = &self.ledger;
        observe(&*self.clock, &mut store).map_err(|_| Unavailable)
    }
}

impl<L, C> Backend for LedgerBackend<L, C>
where
    L: ApprovalLedger + Send + Sync,
    for<'a> &'a L: LedgerStore,
    C: EffectiveClock + Send + Sync,
{
    fn request(
        &self,
        request_id: &str,
    ) -> Result<Option<(ApprovalRequest, ApprovalStatus)>, Unavailable> {
        self.ledger
            .approval_request(request_id)
            .map_err(|_| Unavailable)
    }

    fn begin(
        &self,
        op: &CeremonyOp,
        subject: &SubjectId,
        idempotency_key: &str,
    ) -> Result<Result<IssuedChallenge, Refusal>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .begin_ceremony(&CeremonyBegin {
                op,
                subject,
                idempotency_key,
                now,
            })
            .map_err(|_| Unavailable)
    }

    fn finish(
        &self,
        op: &CeremonyOp,
        challenge_id: &str,
        response: &[u8],
        idempotency_key: &str,
    ) -> Result<Result<CeremonyOutcome, Refusal>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .finish_ceremony(&CeremonyFinish {
                op,
                challenge_id,
                response,
                idempotency_key,
                now,
            })
            .map_err(|_| Unavailable)
    }
}
