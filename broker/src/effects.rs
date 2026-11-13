//! The controlled effect adapter interface (M3-04; D-012, D-025 B, D-033 item 2;
//! m1/fixtures/broker/BROKER.md execute step 7 and `present_permit`,
//! LEDGER-SQLITE.md T-adapter and storage rule 6).
//!
//! An effect adapter owns the system the effect happens in and its own store of
//! consumed permit ids, separate from the broker ledger. The broker reaches it
//! only through [`EffectAdapter::apply`]; tests observe the effect store
//! directly, without going through the broker. The local test adapter is
//! `writ_ledger_sqlite::SqliteEffectStore` (a separate SQLite file whose
//! effects are records, not external side effects).
//!
//! **The adapter transaction** (one logical transaction named `adapter`):
//!
//! 1. Begin. If it cannot begin, the answer is `LEDGER_UNAVAILABLE` and no
//!    permit check result is disclosed (D-025 B).
//! 2. Inside it, the broker's verification runs (the `verify` callback):
//!    `PERMIT_REQUEST_MISMATCH`, `PERMIT_INVALID`, `TIME_UNAVAILABLE`,
//!    `PERMIT_EXPIRED`, `PERMIT_STALE` (D-033), in that order
//!    ([`crate::permit::Permit::verify`]); any refusal rolls back.
//! 3. The verified permit id is already recorded as consumed: `PERMIT_REPLAYED`
//!    (rolls back).
//! 4. Otherwise the permit id is recorded as consumed and the effect is
//!    applied (or recorded as failed) in the same transaction, then committed.
//!    A failing commit is `LEDGER_UNAVAILABLE` and leaves neither.
//!
//! An adapter therefore applies at most one effect per permit id, and never an
//! effect without a consumed permit. It does not make an external system
//! exactly-once: after a failure the broker cannot know whether the commit
//! happened, so the receipt stays `dispatched` and only reconciliation may
//! proceed. At-most-once dispatch is distinct from exactly-once external
//! effects (D-035 item 2).
//!
//! **Inspection** ([`EffectAdapter::inspect`], D-035 item 3): a client-held
//! permit is verified in a read-only adapter transaction (same precedence:
//! storage first, then the permit checks) and its consumption looked up; it
//! never applies an effect. **Evidence** ([`EffectAdapter::evidence`]): the
//! adapter's durable record of what it did with one permit id, which
//! reconciliation may use as authoritative evidence for the exact bound
//! effect. Absence of a record is not evidence that nothing happened outside
//! the adapter's own store.
//!
//! **Ownership** (D-035 item 2): an adapter's store must not be shared by two
//! brokers or two ledgers. The local SQLite test adapter takes its own
//! identity-safe ownership lock and is bound to one ledger; every future
//! production adapter needs its own explicit concurrency, replay,
//! authorization and outcome-recovery contract (OS file locks are not
//! required for remote adapters).

use crate::permit::VerifiedPermit;
use crate::refusal::Refusal;
use crate::store::TxFailure;
use writ_kernel::json::Value;

/// What the adapter did with a fully verified, newly consumed permit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EffectResult {
    /// The effect was applied (`{"result": "effect_applied"}` for
    /// `present_permit`; the receipt becomes `observed_success`).
    Applied,
    /// The adapter consumed the permit but the effect failed; nothing was
    /// applied (the receipt becomes `observed_failure`).
    Failed,
}

/// A permit that passed the broker's verification for one presented request,
/// with the request's tenant and idempotency key. Built only by the broker
/// inside the adapter transaction; an adapter cannot fabricate one:
///
/// ```compile_fail,E0451
/// let _ = writ_broker::effects::VerifiedEffect {
///     permit_id: String::new(),
///     request_fingerprint: String::new(),
///     tenant_id: String::new(),
///     idempotency_key: String::new(),
/// };
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedEffect {
    permit_id: String,
    request_fingerprint: String,
    tenant_id: String,
    idempotency_key: String,
}

impl VerifiedEffect {
    /// The effect of `permit` for the presented `request` (whose fingerprint
    /// the permit verification already matched). A request without a string
    /// `tenant_id` and `idempotency_key` cannot be attributed: `PERMIT_INVALID`.
    pub(crate) fn new(permit: VerifiedPermit, request: &Value) -> Result<VerifiedEffect, Refusal> {
        let text = |k: &str| {
            request
                .get(k)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or(Refusal::PermitInvalid)
        };
        Ok(VerifiedEffect {
            tenant_id: text("tenant_id")?,
            idempotency_key: text("idempotency_key")?,
            permit_id: permit.permit_id,
            request_fingerprint: permit.request_fingerprint,
        })
    }

    /// The permit id to record as consumed.
    pub fn permit_id(&self) -> &str {
        &self.permit_id
    }

    /// D-012 fingerprint of the request the effect is for.
    pub fn request_fingerprint(&self) -> &str {
        &self.request_fingerprint
    }

    /// Tenant of the request.
    pub fn tenant_id(&self) -> &str {
        &self.tenant_id
    }

    /// Idempotency key of the request.
    pub fn idempotency_key(&self) -> &str {
        &self.idempotency_key
    }
}

/// The broker's verification, run by the adapter inside its transaction.
pub type Verify<'v> = dyn FnMut() -> Result<VerifiedEffect, Refusal> + 'v;

/// The adapter's durable record of one consumed permit: the effect it was
/// bound to and what happened (applied, or consumed with a failed effect).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EffectEvidence {
    /// The consumed permit id.
    pub permit_id: String,
    /// Tenant of the request the permit was consumed for.
    pub tenant_id: String,
    /// Idempotency key of that request.
    pub idempotency_key: String,
    /// D-012 fingerprint of that request.
    pub request_fingerprint: String,
    /// What the adapter did.
    pub result: EffectResult,
}

impl EffectEvidence {
    /// `true` when the record is for exactly this receipt: same tenant,
    /// idempotency key and request fingerprint.
    pub fn binds(&self, tenant_id: &str, idempotency_key: &str, fingerprint: &str) -> bool {
        self.tenant_id == tenant_id
            && self.idempotency_key == idempotency_key
            && self.request_fingerprint == fingerprint
    }
}

/// A controlled effect adapter with its own store (module documentation).
pub trait EffectAdapter: Sync {
    /// Run the adapter transaction: begin; `verify`; replay check; record the
    /// consumed permit id and apply the effect atomically; commit. The outer
    /// `Err` is a failed transaction (with its stage, and whether an issued
    /// commit has an unknown outcome); the inner `Err` is a permit refusal
    /// (rolled back, nothing recorded).
    fn apply(&self, verify: &mut Verify<'_>) -> Result<Result<EffectResult, Refusal>, TxFailure>;

    /// Inspect a presented permit without applying anything: begin a
    /// read-only transaction; `verify`; return the evidence recorded for the
    /// verified permit id (`None`: not consumed); roll back. A storage
    /// failure is the outer `Err` and takes precedence over every permit
    /// check (D-025 B, D-035 item 4).
    fn inspect(
        &self,
        verify: &mut Verify<'_>,
    ) -> Result<Result<Option<EffectEvidence>, Refusal>, TxFailure>;

    /// The evidence recorded for `permit_id`, if any (read-only transaction).
    fn evidence(&self, permit_id: &str) -> Result<Option<EffectEvidence>, TxFailure>;
}
