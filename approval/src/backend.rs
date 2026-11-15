//! What the approval page needs from the broker (M4-11; D-040 O-2, O-3, O-11; I-2; D-044
//! item 3).
//!
//! The listener sees the ledger only through [`Backend`]: the one protected read of stored
//! record context ([`Backend::read`], the ledger's `read_record`: an authenticated read
//! session, its subject, credential and scoped permission rechecked inside the read
//! transaction; one refusal for every failure, D-044 Z3), the two ceremony transactions
//! (including the read-session login, Z1) and the operator-bound registration finish
//! (D-043 Y2; the row itself is created by the operator's `RegistrationStart` ceremony).
//! There is no unauthenticated read of a stored request, a pending signoff or a ceremony
//! context. The operation, its target and the idempotency key are the listener's
//! server-side state; the verifier, its relying party and the channel identity are the
//! ledger's configured [`writ_broker::human::ApprovalChannel`] (O-2, M1), never a call
//! argument. [`LedgerBackend`] adapts any [`ApprovalLedger`] with a trusted clock.

use std::sync::Arc;

use writ_broker::approval_store::{
    ApprovalLedger, CeremonyBegin, CeremonyFinish, CeremonyOp, CeremonyOutcome, IssuedChallenge,
    OperatorSession, ReadRefused, ReadSession, ReadTarget, RecordRead, RecordView,
    RegistrationFinish, RegistrationLedger,
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
    /// The one protected read (the ledger's `read_record`, D-044 Z3): `target` for the
    /// authenticated read session `session`, or the one [`ReadRefused`].
    fn read(
        &self,
        session: &ReadSession,
        target: &ReadTarget,
    ) -> Result<Result<RecordView, ReadRefused>, Unavailable>;

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

    /// Finish registration row `registration_id` in `operator`'s page session (D-043 Y2).
    fn finish_registration(
        &self,
        registration_id: &str,
        response: &[u8],
        operator: &OperatorSession,
    ) -> Result<Result<VerifiedRegistration, Refusal>, Unavailable>;

    /// The distinct byte lengths of the active credential ids (the ledger's
    /// `credential_id_lengths`; aggregate metadata for the login decoys, D-044 review M1).
    fn credential_id_lengths(&self) -> Result<Vec<usize>, Unavailable>;
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
    fn read(
        &self,
        session: &ReadSession,
        target: &ReadTarget,
    ) -> Result<Result<RecordView, ReadRefused>, Unavailable> {
        let now = self.now()?;
        self.ledger
            .read_record(&RecordRead {
                session,
                target,
                now,
            })
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

    fn credential_id_lengths(&self) -> Result<Vec<usize>, Unavailable> {
        self.ledger.credential_id_lengths().map_err(|_| Unavailable)
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
