//! What the approval page needs from the broker (M4-11; D-040 O-2, O-3, O-11; I-2).
//!
//! The listener sees the ledger only through [`Backend`]: reads of a stored approval
//! request, of a pending signoff and of the context any ceremony would bind (its
//! fingerprint-covered context, R6), the two ceremony transactions and the two
//! operator-bound registration finish (D-043 Y2; the row itself is created by the
//! operator's `RegistrationStart` ceremony). The
//! operation, its target and the idempotency key are the listener's server-side state;
//! the verifier, its relying party and the channel identity are the ledger's configured
//! [`writ_broker::human::ApprovalChannel`] (O-2, M1), never a call argument.
//! [`LedgerBackend`] adapts any [`ApprovalLedger`] with a trusted clock.

use std::sync::Arc;

use writ_broker::approval::{ApprovalRequest, ApprovalStatus, NewSignoff};
use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyContext, CeremonyFinish, CeremonyOp, CeremonyOutcome,
    IssuedChallenge, OperatorSession, RegistrationFinish, RegistrationLedger,
};
use writ_broker::clock::{observe, EffectiveClock, ObservedTime};
use writ_broker::human::{SubjectId, VerifiedRegistration};
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

    /// The context a ceremony for `op` would bind (the ledger's `ceremony_context`; a
    /// read).
    fn context(&self, op: &CeremonyOp) -> Result<Result<CeremonyContext, Refusal>, Unavailable>;

    /// The signoff binding offered for `signoff_id` (the ledger's `pending_signoff`; a
    /// read).
    fn pending_signoff(&self, signoff_id: &str) -> Result<Option<NewSignoff>, Unavailable>;

    /// Finish registration row `registration_id` in `operator`'s page session (D-043 Y2).
    fn finish_registration(
        &self,
        registration_id: &str,
        response: &[u8],
        operator: &OperatorSession,
    ) -> Result<Result<VerifiedRegistration, Refusal>, Unavailable>;
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
    L: ApprovalLedger + RegistrationLedger + Send + Sync,
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

    fn context(&self, op: &CeremonyOp) -> Result<Result<CeremonyContext, Refusal>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .ceremony_context(op, now)
            .map_err(|_| Unavailable)
    }

    fn pending_signoff(&self, signoff_id: &str) -> Result<Option<NewSignoff>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .pending_signoff(signoff_id, now)
            .map_err(|_| Unavailable)
    }

    fn finish_registration(
        &self,
        registration_id: &str,
        response: &[u8],
        operator: &OperatorSession,
    ) -> Result<Result<VerifiedRegistration, Refusal>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .finish_registration(&RegistrationFinish {
                registration_id,
                response,
                operator: Some(operator),
                now,
            })
            .map_err(|_| Unavailable)
    }
}
