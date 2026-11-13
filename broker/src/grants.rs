//! Approval grants and their atomic single-use consumption (M3-03; D-009,
//! D-012, D-018 A6, D-025 C-3 and C-4, D-029; m1/fixtures/broker/BROKER.md
//! `approve` and execute step 5, LEDGER-SQLITE.md `approve` and T-reserve).
//!
//! **Approval issuance** ([`GrantService::approve`]). An approval is presented
//! on a separate trusted channel, never in an agent request: an
//! [`ApprovalAuthority`] (no production implementation yet; the approval
//! service is M4) authenticates the approver on that channel. The broker then
//! refuses, in this order and before touching the ledger:
//! `HUMAN_UNAUTHENTICATED` (the authority refused, or answered for another
//! channel or identity than presented, or the principal is not a human: agent,
//! model and service identities are never accepted); `NOT_APPROVABLE` (the
//! use-time decision for the request to approve is not `require_approval`);
//! `SELF_APPROVAL_FORBIDDEN` (the approver is the request's principal or
//! agent). Otherwise the `approve` transaction inserts a single-use grant
//! (`max_uses` 1) whose binding is the [`GrantBinding`] of the request and its
//! use-time decision with the given verdict, expiring `ttl` seconds (default
//! 600) after the trusted time of the decision; a label already in use is
//! `GRANT_REF_IN_USE` and the existing grant is never reset or replenished.
//!
//! **Consumption** ([`GrantService::reserve`], or
//! [`GrantService::reserve_authorized`] under a hold the caller keeps). Under
//! one shared hold of the policy gate: trusted time is observed (the floor is
//! persisted in its own fenced transaction before anything is decided), the
//! request is re-decided at use time, `require_approval` without a grant is
//! `APPROVAL_REQUIRED`, then the per-key lock is taken and the T-reserve
//! transaction runs (see [`crate::store::GrantLedger::reserve`]). Inside it an
//! existing receipt gives `IDEMPOTENCY_CONFLICT`, a replay, or
//! `RECONCILIATION_REQUIRED`, or resumes a `reserved` receipt without a second
//! grant use; a new reservation checks the grant (`GRANT_UNKNOWN`, C-4
//! integrity, `GRANT_MISMATCH`, `GRANT_EXPIRED` on the observed time,
//! `GRANT_EXHAUSTED`), consumes exactly one use and reserves the receipt.
//! Every refusal of the two service entry points is audited in its own
//! transaction after the operation's other transactions ended; an audit
//! failure does not change the answer (LEDGER-SQLITE.md storage rule 2).
//!
//! **Lock order (D-029):** policy gate (shared), then the trusted-time floor
//! transaction (completed before anything else is acquired), then the per-key
//! lock ([`KeyLocks`]), then the T-reserve transaction. Nothing waits for the
//! gate while holding a key lock or a transaction. All locks are released on
//! every return path when their guards drop.
//!
//! **Production differences from M1** (frozen M1 expectations unchanged):
//! - The request to approve is a sealed [`VerifiedRequest`] (M1 `for_request`
//!   was an unsealed request); the approver is authenticated by an
//!   [`ApprovalAuthority`] instead of a credential match on a fixture channel,
//!   and a non-human principal is refused as `HUMAN_UNAUTHENTICATED`.
//! - Time is the trusted effective time observed through
//!   [`crate::clock::observe`] (floor persisted first), not an injected `now`.
//!   The decision, the grant expiry at approval and the expiry check at
//!   consumption all use the one observed instant of their operation.
//! - The verdict is the closed set `approved` / `denied` of the ApprovalGrant
//!   contract (M1 accepted any string; only `approved` ever matched), labels
//!   are 1 to 128 characters and the lifetime is a non-negative `u32` of
//!   seconds (M1 accepted any integer).
//! - The C-4 integrity diagnostic code is `integrity` (M1 reported
//!   `SQLITE_CORRUPT`, which D-029 reserves for genuine SQLite corruption).
//! - The approver identity and channel are checked but not persisted with the
//!   grant (the `writ-ledger-2` grants table has no column for them).

use std::collections::HashSet;
use std::sync::{Condvar, Mutex, PoisonError};

use writ_kernel::contracts::StateRevision;
use writ_kernel::json::{self, Value};
use writ_kernel::time::Timestamp;

use crate::authorize::{authorize, Authorized, UseTimeStatus};
use crate::clock::{observe, EffectiveClock, ObservedTime};
use crate::ingress::VerifiedRequest;
use crate::policy::ActivePolicy;
use crate::refusal::Refusal;
use crate::store::{GrantLedger, LedgerStore};

/// Grant lifetime when the approval names none (BROKER.md `approve`).
pub const DEFAULT_GRANT_TTL_SECONDS: u32 = 600;
/// Uses of every issued grant (single use).
pub const GRANT_MAX_USES: u32 = 1;
/// Maximum length of a grant label.
pub const MAX_GRANT_LABEL_LEN: usize = 128;

/// The members of a stored grant binding (LEDGER-SQLITE.md storage rule 5).
pub const BINDING_MEMBERS: [&str; 9] = [
    "action_digest",
    "action_kind",
    "approval_request_id",
    "canonical_resource_id",
    "policy_bundle_sha",
    "principal_id",
    "requested_capabilities",
    "tenant_id",
    "verdict",
];

/// The label naming one grant for its whole life: 1 to 128 characters.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct GrantLabel(String);

impl GrantLabel {
    /// `Some` for 1 to [`MAX_GRANT_LABEL_LEN`] characters.
    pub fn new(label: &str) -> Option<GrantLabel> {
        (!label.is_empty() && label.chars().count() <= MAX_GRANT_LABEL_LEN)
            .then(|| GrantLabel(label.to_string()))
    }

    /// The label text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The human's verdict (ApprovalGrant contract). A `denied` grant is stored
/// and never matches a request (fixture b-21).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// The human approved the request.
    Approved,
    /// The human denied the request.
    Denied,
}

impl Verdict {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Approved => "approved",
            Verdict::Denied => "denied",
        }
    }
}

/// What kind of principal an [`ApprovalAuthority`] authenticated. Only
/// [`PrincipalKind::Human`] may approve.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrincipalKind {
    /// A human.
    Human,
    /// An agent (never an approver).
    Agent,
    /// A model (never an approver).
    Model,
    /// A service or automation account (never an approver).
    Service,
}

/// An approval as presented on a trusted approval channel (never an agent
/// request field). The credential is interpreted only by the authority.
#[derive(Clone, Copy, Debug)]
pub struct ApprovalPresentation<'a> {
    /// Identity of the channel the approval arrived on.
    pub channel: &'a str,
    /// The approver identity claimed on that channel.
    pub approver: &'a str,
    /// Authority-specific proof of the approver's identity.
    pub credential: &'a [u8],
}

/// A principal the [`ApprovalAuthority`] authenticated.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthenticatedApprover {
    /// Principal id.
    pub id: String,
    /// Principal kind.
    pub kind: PrincipalKind,
    /// The trusted channel the authority authenticated it on.
    pub channel: String,
}

/// Why an approver was not accepted. Every variant is `HUMAN_UNAUTHENTICATED`;
/// the variant is an operator diagnostic only.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApproverRefusal {
    /// The channel is not a trusted approval channel.
    UntrustedChannel,
    /// The credential does not authenticate the claimed approver on the channel.
    Unauthenticated,
    /// The authority answered for another channel than the one presented.
    ChannelMismatch,
    /// The authority answered for another identity than the one claimed.
    IdentityMismatch,
    /// The principal is authenticated but is not a human.
    NotHuman,
}

/// Authenticates approvers on trusted approval channels (D-009: an
/// authenticated human principal on a channel separate from agent input).
/// There is no production implementation yet; tests provide one.
pub trait ApprovalAuthority {
    /// The principal `presentation` authenticates, or why not.
    fn authenticate(
        &self,
        presentation: &ApprovalPresentation<'_>,
    ) -> Result<AuthenticatedApprover, ApproverRefusal>;
}

/// The authority's answer, checked against the presentation: same channel,
/// same identity, a human.
fn authenticate(
    authority: &dyn ApprovalAuthority,
    presentation: &ApprovalPresentation<'_>,
) -> Result<AuthenticatedApprover, ApproverRefusal> {
    let approver = authority.authenticate(presentation)?;
    if approver.channel != presentation.channel {
        return Err(ApproverRefusal::ChannelMismatch);
    }
    if approver.id != presentation.approver {
        return Err(ApproverRefusal::IdentityMismatch);
    }
    if approver.kind != PrincipalKind::Human {
        return Err(ApproverRefusal::NotHuman);
    }
    Ok(approver)
}

/// The full request binding of a grant (BROKER.md execute step 5.3): verdict,
/// approval request id, tenant, requester principal, action kind, canonical
/// resource, action digest, sorted capability set and bundle digest. Stored
/// and compared as D-006 canonical JSON.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GrantBinding {
    verdict: Verdict,
    approval_request_id: String,
    tenant_id: String,
    principal_id: String,
    action_kind: String,
    canonical_resource_id: String,
    action_digest: String,
    requested_capabilities: Vec<String>,
    policy_bundle_sha: String,
}

impl GrantBinding {
    /// The binding of `authorized` with `verdict`; `None` unless the use-time
    /// decision is `require_approval` with an approval request id and a
    /// canonical resource id (the kernel always provides both for that
    /// status; their absence fails closed).
    pub fn for_decision(authorized: &Authorized<'_>, verdict: Verdict) -> Option<GrantBinding> {
        if authorized.status() != UseTimeStatus::RequireApproval {
            return None;
        }
        let request = authorized.request();
        Some(GrantBinding {
            verdict,
            approval_request_id: authorized.decision().approval_request_reference.clone()?,
            tenant_id: request.tenant_id.clone(),
            principal_id: request.principal.id.clone(),
            action_kind: request.action.kind.clone(),
            canonical_resource_id: request.action.resource.canonical_resource_id.clone()?,
            action_digest: request.action.action_digest.clone(),
            requested_capabilities: request.capability_set(),
            policy_bundle_sha: authorized.policy_bundle_sha().to_string(),
        })
    }

    /// The binding as a JSON object with exactly [`BINDING_MEMBERS`].
    pub fn to_value(&self) -> Value {
        json::obj([
            ("action_digest", json::s(&self.action_digest)),
            ("action_kind", json::s(&self.action_kind)),
            ("approval_request_id", json::s(&self.approval_request_id)),
            (
                "canonical_resource_id",
                json::s(&self.canonical_resource_id),
            ),
            ("policy_bundle_sha", json::s(&self.policy_bundle_sha)),
            ("principal_id", json::s(&self.principal_id)),
            (
                "requested_capabilities",
                Value::Arr(
                    self.requested_capabilities
                        .iter()
                        .map(|c| json::s(c))
                        .collect(),
                ),
            ),
            ("tenant_id", json::s(&self.tenant_id)),
            ("verdict", json::s(self.verdict.as_str())),
        ])
    }

    /// The D-006 canonical text (`None` only outside the D-006 domain).
    pub fn canonical_text(&self) -> Option<String> {
        json::canonical(&self.to_value()).ok()
    }
}

/// Stored-grant integrity (LEDGER-SQLITE.md storage rule 5, D-025 C-4): the
/// text is strict JSON (D-017), exactly its own D-006 canonical text, and an
/// object with exactly [`BINDING_MEMBERS`], all strings except
/// `requested_capabilities`, an array of strings. `false` is a
/// ledger-integrity failure (`LEDGER_UNAVAILABLE`), never a mismatch.
pub fn binding_intact(text: &str) -> bool {
    let Ok(value) = json::parse(text.as_bytes()) else {
        return false;
    };
    if json::canonical(&value).ok().as_deref() != Some(text) {
        return false;
    }
    let Some(members) = value.as_obj() else {
        return false;
    };
    members.len() == BINDING_MEMBERS.len()
        && BINDING_MEMBERS.iter().all(|name| match members.get(*name) {
            Some(Value::Arr(items)) if *name == "requested_capabilities" => {
                items.iter().all(|x| matches!(x, Value::Str(_)))
            }
            Some(Value::Str(_)) => *name != "requested_capabilities",
            _ => false,
        })
}

/// A grant to insert (the `approve` transaction). Built only by the broker
/// ([`GrantService::approve`]); a store cannot fabricate one:
///
/// ```compile_fail,E0451
/// let _ = writ_broker::grants::NewGrant {
///     label: writ_broker::grants::GrantLabel::new("g1").unwrap(),
///     binding: String::new(),
///     expires_at: writ_broker::writ_kernel::time::Timestamp::parse_strict("2026-11-05T12:00:00Z").unwrap(),
///     max_uses: 1,
/// };
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewGrant {
    label: GrantLabel,
    binding: String,
    expires_at: Timestamp,
    max_uses: u32,
}

impl NewGrant {
    /// The grant label.
    pub fn label(&self) -> &GrantLabel {
        &self.label
    }

    /// D-006 canonical text of the [`GrantBinding`].
    pub fn binding(&self) -> &str {
        &self.binding
    }

    /// Expiry (exclusive: expired at and after this instant).
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }

    /// Maximum uses ([`GRANT_MAX_USES`]).
    pub fn max_uses(&self) -> u32 {
        self.max_uses
    }
}

/// Result of the `approve` transaction.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantInsert {
    /// The grant was inserted with no use.
    Inserted,
    /// The label already names a grant; nothing changed.
    LabelInUse,
}

/// A grant as read inside T-reserve.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredGrant {
    /// Stored binding text (checked with [`binding_intact`]).
    pub binding: String,
    /// Stored expiry.
    pub expires_at: Timestamp,
    /// Stored maximum uses.
    pub max_uses: i64,
    /// Stored uses.
    pub uses: i64,
}

/// What [`ReserveRequest::check_grant`] decided for the stored grant.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GrantCheck {
    /// Consume one use (conditional update of exactly one row).
    Consume,
    /// Roll back and refuse.
    Refuse(Refusal),
    /// The stored binding fails [`binding_intact`]: roll back, ledger-integrity failure.
    Integrity,
}

/// Receipt status (D-012; the `receipts.status` values).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReceiptStatus {
    /// Reserved; dispatch has definitely not begun (C-3).
    Reserved,
    /// Dispatched; the effect may have started.
    Dispatched,
    /// Marked uncertain by reconciliation.
    UnknownReconcile,
    /// The effect completed.
    ObservedSuccess,
    /// The effect failed.
    ObservedFailure,
}

impl ReceiptStatus {
    /// The stored text.
    pub fn as_str(self) -> &'static str {
        match self {
            ReceiptStatus::Reserved => "reserved",
            ReceiptStatus::Dispatched => "dispatched",
            ReceiptStatus::UnknownReconcile => "unknown_reconcile",
            ReceiptStatus::ObservedSuccess => "observed_success",
            ReceiptStatus::ObservedFailure => "observed_failure",
        }
    }

    /// The status for its stored text; `None` for any other text.
    pub fn parse(text: &str) -> Option<ReceiptStatus> {
        [
            ReceiptStatus::Reserved,
            ReceiptStatus::Dispatched,
            ReceiptStatus::UnknownReconcile,
            ReceiptStatus::ObservedSuccess,
            ReceiptStatus::ObservedFailure,
        ]
        .into_iter()
        .find(|s| s.as_str() == text)
    }
}

/// A receipt as read inside T-reserve.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredReceipt {
    /// Stored request fingerprint.
    pub fingerprint: String,
    /// Stored effective grant label (`None` for an `allow` decision).
    pub grant_label: Option<String>,
    /// Stored status.
    pub status: ReceiptStatus,
}

/// The stored result an idempotent retry replays.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Replay {
    /// `{"result": "effect_completed", "replayed": true}`.
    EffectCompleted,
    /// `{"result": "effect_failed", "replayed": true}`.
    EffectFailed,
}

/// Successful outcome of T-reserve.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReserveOutcome {
    /// A new reservation committed (with exactly one grant use when a grant
    /// was effective). Dispatch may follow (M3-04).
    Reserved,
    /// The receipt was already `reserved` for this request and grant: resumed
    /// without a second grant use (a write-free commit, D-025 A).
    Resumed,
    /// The effect already has an observed result; nothing changed.
    Replayed(Replay),
}

/// The effective grant of a reservation and what it must match.
#[derive(Clone, Debug, PartialEq, Eq)]
struct GrantUse {
    label: GrantLabel,
    expected: Option<String>,
    now: Timestamp,
}

/// The input of one T-reserve, built only by the broker from a guard-bound
/// [`Authorized`] (it borrows it, so it cannot outlive the hold) and the
/// observed trusted time. Code outside the broker cannot build one:
///
/// ```compile_fail,E0624
/// use writ_broker::authorize::Authorized;
/// use writ_broker::clock::ObservedTime;
/// use writ_broker::grants::ReserveRequest;
///
/// fn forge<'a>(authorized: &'a Authorized<'_>, at: ObservedTime) {
///     let _ = ReserveRequest::new(authorized, at, None);
/// }
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReserveRequest<'a> {
    tenant_id: &'a str,
    idempotency_key: &'a str,
    fingerprint: &'a str,
    grant: Option<GrantUse>,
}

impl<'a> ReserveRequest<'a> {
    /// The reservation for `authorized` at `observed`. The effective grant is
    /// `grant` for `require_approval` (absent: `APPROVAL_REQUIRED`) and none
    /// for `allow`. The expected binding is the `approved` [`GrantBinding`];
    /// if it cannot be built, nothing can match it (`GRANT_MISMATCH`).
    pub(crate) fn new(
        authorized: &'a Authorized<'_>,
        observed: ObservedTime,
        grant: Option<&GrantLabel>,
    ) -> Result<ReserveRequest<'a>, Refusal> {
        let grant = match authorized.status() {
            UseTimeStatus::Allow => None,
            UseTimeStatus::RequireApproval => Some(GrantUse {
                label: grant.ok_or(Refusal::ApprovalRequired)?.clone(),
                expected: GrantBinding::for_decision(authorized, Verdict::Approved)
                    .and_then(|b| b.canonical_text()),
                now: observed.at(),
            }),
        };
        let request = authorized.request();
        Ok(ReserveRequest {
            tenant_id: &request.tenant_id,
            idempotency_key: &request.idempotency_key,
            fingerprint: authorized.verified().fingerprint().as_str(),
            grant,
        })
    }

    /// Tenant of the idempotency key.
    pub fn tenant_id(&self) -> &str {
        self.tenant_id
    }

    /// The idempotency key.
    pub fn idempotency_key(&self) -> &str {
        self.idempotency_key
    }

    /// D-012 fingerprint of the complete request.
    pub fn fingerprint(&self) -> &str {
        self.fingerprint
    }

    /// The effective grant label; `None` for an `allow` decision.
    pub fn grant_label(&self) -> Option<&str> {
        self.grant.as_ref().map(|g| g.label.as_str())
    }

    /// Outcome for an existing receipt of this key (T-reserve): another
    /// fingerprint or effective grant is `IDEMPOTENCY_CONFLICT`; otherwise by
    /// status: replay, `RECONCILIATION_REQUIRED`, or resume from `reserved`
    /// (the only outcome the store commits, writing nothing).
    pub fn existing(&self, stored: &StoredReceipt) -> Result<ReserveOutcome, Refusal> {
        if stored.fingerprint != self.fingerprint
            || stored.grant_label.as_deref() != self.grant_label()
        {
            return Err(Refusal::IdempotencyConflict);
        }
        match stored.status {
            ReceiptStatus::ObservedSuccess => Ok(ReserveOutcome::Replayed(Replay::EffectCompleted)),
            ReceiptStatus::ObservedFailure => Ok(ReserveOutcome::Replayed(Replay::EffectFailed)),
            ReceiptStatus::Dispatched | ReceiptStatus::UnknownReconcile => {
                Err(Refusal::ReconciliationRequired)
            }
            ReceiptStatus::Reserved => Ok(ReserveOutcome::Resumed),
        }
    }

    /// Check the effective grant's stored row (`None`: no such label), in the
    /// order `GRANT_UNKNOWN`, integrity (C-4), `GRANT_MISMATCH` (canonical
    /// text comparison), `GRANT_EXPIRED` (observed time at or after expiry),
    /// `GRANT_EXHAUSTED`. Without an effective grant nothing may be consumed
    /// (`GRANT_UNKNOWN`).
    pub fn check_grant(&self, stored: Option<&StoredGrant>) -> GrantCheck {
        let (Some(grant), Some(stored)) = (&self.grant, stored) else {
            return GrantCheck::Refuse(Refusal::GrantUnknown);
        };
        if !binding_intact(&stored.binding) {
            return GrantCheck::Integrity;
        }
        if grant.expected.as_deref() != Some(stored.binding.as_str()) {
            return GrantCheck::Refuse(Refusal::GrantMismatch);
        }
        if grant.now >= stored.expires_at {
            return GrantCheck::Refuse(Refusal::GrantExpired);
        }
        if stored.uses >= stored.max_uses {
            return GrantCheck::Refuse(Refusal::GrantExhausted);
        }
        GrantCheck::Consume
    }
}

/// In-process per-key locks (LEDGER-SQLITE.md T-reserve step 2): operations
/// on one (tenant, idempotency key) never interleave inside the process;
/// different keys run in parallel. Volatile, process-local (D-029 limitation),
/// and taken after the policy gate and before any ledger transaction.
#[derive(Debug, Default)]
pub struct KeyLocks {
    held: Mutex<HashSet<(String, String)>>,
    released: Condvar,
}

/// One held key lock; released (and its entry removed) when dropped.
#[derive(Debug)]
pub struct KeyGuard<'a> {
    locks: &'a KeyLocks,
    key: (String, String),
}

impl KeyLocks {
    /// No key held.
    pub fn new() -> KeyLocks {
        KeyLocks::default()
    }

    /// Wait until no other holder has (`tenant`, `key`), then hold it.
    pub fn lock(&self, tenant: &str, key: &str) -> KeyGuard<'_> {
        let key = (tenant.to_string(), key.to_string());
        // The set is only inserted into and removed from; it is consistent
        // even if a holder panicked, so a poisoned mutex is recovered.
        let mut held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
        while held.contains(&key) {
            held = self
                .released
                .wait(held)
                .unwrap_or_else(PoisonError::into_inner);
        }
        held.insert(key.clone());
        KeyGuard { locks: self, key }
    }

    /// Number of keys currently held (diagnostics and tests).
    pub fn held(&self) -> usize {
        self.held
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .len()
    }
}

impl Drop for KeyGuard<'_> {
    fn drop(&mut self) {
        let mut held = self
            .locks
            .held
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        held.remove(&self.key);
        drop(held);
        self.locks.released.notify_all();
    }
}

/// An approval to issue (BROKER.md `approve`).
#[derive(Clone, Copy, Debug)]
pub struct Approval<'a> {
    /// The sealed request the human approves.
    pub request: &'a VerifiedRequest,
    /// Trusted kernel state revision for the use-time decision.
    pub state_revision: StateRevision,
    /// Label of the new grant.
    pub label: &'a GrantLabel,
    /// The human's verdict.
    pub verdict: Verdict,
    /// Lifetime in seconds; `None` is [`DEFAULT_GRANT_TTL_SECONDS`].
    pub ttl_seconds: Option<u32>,
}

/// A grant issued by [`GrantService::approve`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GrantIssued {
    /// Its label.
    pub label: GrantLabel,
    /// Its expiry.
    pub expires_at: Timestamp,
    /// Its binding (D-006 canonical text).
    pub binding: String,
}

/// Approval issuance and grant consumption over one shared ledger.
///
/// One service is shared by every thread of the owning process (it is `Sync`
/// when the ledger is): it owns the process's [`KeyLocks`] and borrows the
/// active policy, the trusted clock and the ledger. The ledger type `L`
/// provides the grant transactions ([`GrantLedger`]) and, through `&L`, the
/// trusted-time floor and refusal audit ([`LedgerStore`]).
pub struct GrantService<'a, L: ?Sized> {
    active: &'a ActivePolicy,
    clock: &'a (dyn EffectiveClock + Sync),
    ledger: &'a L,
    key_locks: KeyLocks,
}

impl<'a, L> GrantService<'a, L>
where
    L: GrantLedger + ?Sized,
    for<'b> &'b L: LedgerStore,
{
    /// A service over `ledger`, deciding under `active` with time from `clock`.
    pub fn new(
        active: &'a ActivePolicy,
        clock: &'a (dyn EffectiveClock + Sync),
        ledger: &'a L,
    ) -> GrantService<'a, L> {
        GrantService {
            active,
            clock,
            ledger,
            key_locks: KeyLocks::new(),
        }
    }

    /// The per-key locks of this service.
    pub fn key_locks(&self) -> &KeyLocks {
        &self.key_locks
    }

    /// Read trusted time and persist it as the ledger's floor in its own
    /// fenced transaction ([`observe`]); unavailable time is `TIME_UNAVAILABLE`.
    pub fn observe(&self) -> Result<ObservedTime, Refusal> {
        let mut ledger: &L = self.ledger;
        observe(self.clock, &mut ledger).map_err(|e| e.refusal())
    }

    /// Audit `result`'s refusal in its own transaction (failure ignored:
    /// the answer stays the refusal, storage rule 2).
    fn audited<T>(&self, result: Result<T, Refusal>) -> Result<T, Refusal> {
        if let Err(refusal) = &result {
            let mut ledger: &L = self.ledger;
            let _ = LedgerStore::record_refusal(&mut ledger, refusal.code());
        }
        result
    }

    /// Issue a grant for `approval` presented as `presentation`, authenticated
    /// by `authority` (module documentation for the order of refusals).
    /// Authentication refusals happen before any ledger access other than the
    /// refusal audit.
    pub fn approve(
        &self,
        approval: &Approval<'_>,
        presentation: &ApprovalPresentation<'_>,
        authority: &dyn ApprovalAuthority,
    ) -> Result<GrantIssued, Refusal> {
        let result = self.approve_unaudited(approval, presentation, authority);
        self.audited(result)
    }

    fn approve_unaudited(
        &self,
        approval: &Approval<'_>,
        presentation: &ApprovalPresentation<'_>,
        authority: &dyn ApprovalAuthority,
    ) -> Result<GrantIssued, Refusal> {
        let approver =
            authenticate(authority, presentation).map_err(Refusal::HumanUnauthenticated)?;
        let shared = self.active.shared();
        let observed = self.observe()?;
        let inputs = shared.trusted_inputs(approval.state_revision);
        let authorized = match authorize(approval.request, &inputs, &observed) {
            Ok(authorized) => authorized,
            Err(Refusal::Decision { .. }) => return Err(Refusal::NotApprovable),
            Err(other) => return Err(other),
        };
        if authorized.status() != UseTimeStatus::RequireApproval {
            return Err(Refusal::NotApprovable);
        }
        let request = authorized.request();
        if approver.id == request.principal.id
            || request.agent_id.as_deref() == Some(approver.id.as_str())
        {
            return Err(Refusal::SelfApprovalForbidden);
        }
        let binding = GrantBinding::for_decision(&authorized, approval.verdict)
            .and_then(|b| b.canonical_text())
            .ok_or(Refusal::NotApprovable)?;
        let ttl = approval.ttl_seconds.unwrap_or(DEFAULT_GRANT_TTL_SECONDS);
        let grant = NewGrant {
            label: approval.label.clone(),
            binding,
            expires_at: observed.at().plus_seconds(i64::from(ttl)),
            max_uses: GRANT_MAX_USES,
        };
        match self
            .ledger
            .insert_grant(&grant)
            .map_err(Refusal::LedgerUnavailable)?
        {
            GrantInsert::Inserted => Ok(GrantIssued {
                label: grant.label,
                expires_at: grant.expires_at,
                binding: grant.binding,
            }),
            GrantInsert::LabelInUse => Err(Refusal::GrantRefInUse),
        }
    }

    /// Re-decide `verified` at use time and reserve its receipt, consuming
    /// one use of `grant` when the decision is `require_approval` (BROKER.md
    /// execute steps 3 to 5; no dispatch). Takes the policy gate shared for
    /// the whole operation and audits a refusal after releasing it.
    pub fn reserve(
        &self,
        verified: &VerifiedRequest,
        state_revision: StateRevision,
        grant: Option<&GrantLabel>,
    ) -> Result<ReserveOutcome, Refusal> {
        let result = (|| {
            let shared = self.active.shared();
            let observed = self.observe()?;
            let inputs = shared.trusted_inputs(state_revision);
            let authorized = authorize(verified, &inputs, &observed)?;
            self.reserve_authorized(&authorized, observed, grant)
        })();
        self.audited(result)
    }

    /// T-reserve for a request already decided under a hold the caller keeps
    /// (for M3-04, which dispatches under the same hold). `authorized` must
    /// have been decided at `observed` (otherwise `TIME_UNAVAILABLE`: the
    /// expiry is only ever decided on a persisted observation). Takes the
    /// per-key lock, then runs the transaction. Does not audit.
    pub fn reserve_authorized(
        &self,
        authorized: &Authorized<'_>,
        observed: ObservedTime,
        grant: Option<&GrantLabel>,
    ) -> Result<ReserveOutcome, Refusal> {
        if authorized.decision().decided_at != observed.at() {
            return Err(Refusal::TimeUnavailable);
        }
        let request = ReserveRequest::new(authorized, observed, grant)?;
        let _key = self
            .key_locks
            .lock(request.tenant_id(), request.idempotency_key());
        self.ledger
            .reserve(&request)
            .map_err(Refusal::LedgerUnavailable)?
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::clock::FixedClock;
    use crate::policy::PolicyState;
    use crate::test_support::*;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    fn decided<T>(req: &Value, f: impl FnOnce(&Authorized<'_>) -> T) -> T {
        let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
        active
            .execute(&verify(req), revision(5), &FixedClock::new(now()), |a| {
                f(&a)
            })
            .unwrap()
    }

    fn approved_binding(req: &Value) -> Option<String> {
        decided(req, |a| {
            GrantBinding::for_decision(a, Verdict::Approved).and_then(|b| b.canonical_text())
        })
    }

    #[test]
    fn binding_is_the_full_request_binding_in_canonical_form() {
        let req = publish_request();
        let (text, apr, sha) = decided(&req, |a| {
            (
                GrantBinding::for_decision(a, Verdict::Approved)
                    .and_then(|b| b.canonical_text())
                    .unwrap(),
                a.decision().approval_request_reference.clone().unwrap(),
                a.policy_bundle_sha().to_string(),
            )
        });
        let expected = json::obj([
            (
                "action_digest",
                json::s("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ),
            ("action_kind", json::s("repository.publish")),
            ("approval_request_id", json::s(&apr)),
            (
                "canonical_resource_id",
                json::s("repository:fixture-tenant/fixture-project/main"),
            ),
            ("policy_bundle_sha", json::s(&sha)),
            ("principal_id", json::s("agent:claude-session-1")),
            ("requested_capabilities", Value::Arr(vec![])),
            ("tenant_id", json::s("fixture-tenant")),
            ("verdict", json::s("approved")),
        ]);
        assert_eq!(text, json::canonical(&expected).unwrap());
        assert!(binding_intact(&text));
    }

    #[test]
    fn binding_exists_only_for_require_approval() {
        assert!(approved_binding(&base_request()).is_none(), "allow");
        assert!(approved_binding(&publish_request()).is_some());
        let denied = decided(&publish_request(), |a| {
            GrantBinding::for_decision(a, Verdict::Denied)
                .and_then(|b| b.canonical_text())
                .unwrap()
        });
        assert_ne!(Some(denied.clone()), approved_binding(&publish_request()));
        assert!(denied.contains(r#""verdict":"denied""#));
    }

    #[test]
    fn binding_integrity_rejects_every_malformation() {
        let good = approved_binding(&publish_request()).unwrap();
        assert!(binding_intact(&good));
        let value = json::parse(good.as_bytes()).unwrap();
        let bad = [
            "not json".to_string(),
            String::new(),
            format!("{good} "),
            good.replacen(':', ": ", 1),
            reorder_text(&value),
            json::canonical(&value.without("verdict")).unwrap(),
            json::canonical(&with(&value, &["extra"], json::s("x"))).unwrap(),
            json::canonical(&with(&value, &["verdict"], Value::Int(1))).unwrap(),
            json::canonical(&with(&value, &["verdict"], Value::Null)).unwrap(),
            json::canonical(&with(
                &value,
                &["requested_capabilities"],
                Value::Arr(vec![Value::Int(1)]),
            ))
            .unwrap(),
            json::canonical(&with(&value, &["requested_capabilities"], json::s("x"))).unwrap(),
            good.replacen("{", r#"{"verdict":"approved","#, 1),
            "[]".to_string(),
            r#""approved""#.to_string(),
        ];
        for text in bad {
            assert!(!binding_intact(&text), "{text}");
        }
    }

    #[test]
    fn grant_check_order_and_boundaries() {
        let req = publish_request();
        decided(&req, |a| {
            let observed = {
                let mut l = MemoryLedger::default();
                observe(&FixedClock::new(now()), &mut l).unwrap()
            };
            let label = GrantLabel::new("g1").unwrap();
            let r = ReserveRequest::new(a, observed, Some(&label)).unwrap();
            let good = GrantBinding::for_decision(a, Verdict::Approved)
                .and_then(|b| b.canonical_text())
                .unwrap();
            let grant = |binding: &str, expires: i64, uses: i64| StoredGrant {
                binding: binding.to_string(),
                expires_at: now().plus_seconds(expires),
                max_uses: 1,
                uses,
            };
            assert_eq!(
                r.check_grant(None),
                GrantCheck::Refuse(Refusal::GrantUnknown)
            );
            assert_eq!(
                r.check_grant(Some(&grant("{", 0, 9))),
                GrantCheck::Integrity
            );
            let other = good.replace("fixture-tenant", "other-tenant");
            assert_eq!(
                r.check_grant(Some(&grant(&other, 0, 9))),
                GrantCheck::Refuse(Refusal::GrantMismatch)
            );
            assert_eq!(
                r.check_grant(Some(&grant(&good, 0, 9))),
                GrantCheck::Refuse(Refusal::GrantExpired),
                "expiry is exclusive"
            );
            assert_eq!(
                r.check_grant(Some(&grant(&good, 1, 1))),
                GrantCheck::Refuse(Refusal::GrantExhausted)
            );
            assert_eq!(
                r.check_grant(Some(&grant(&good, 1, 0))),
                GrantCheck::Consume
            );
        });
    }

    #[test]
    fn existing_receipt_outcomes() {
        let req = publish_request();
        decided(&req, |a| {
            let mut l = MemoryLedger::default();
            let observed = observe(&FixedClock::new(now()), &mut l).unwrap();
            let label = GrantLabel::new("g1").unwrap();
            let r = ReserveRequest::new(a, observed, Some(&label)).unwrap();
            let receipt = |fp: &str, grant: Option<&str>, status| StoredReceipt {
                fingerprint: fp.to_string(),
                grant_label: grant.map(str::to_string),
                status,
            };
            let fp = r.fingerprint().to_string();
            use ReceiptStatus::*;
            let cases = [
                (
                    receipt("sha256:other", Some("g1"), Reserved),
                    Err(Refusal::IdempotencyConflict),
                ),
                (
                    receipt(&fp, Some("g2"), Reserved),
                    Err(Refusal::IdempotencyConflict),
                ),
                (
                    receipt(&fp, None, Reserved),
                    Err(Refusal::IdempotencyConflict),
                ),
                (
                    receipt(&fp, Some("g1"), Reserved),
                    Ok(ReserveOutcome::Resumed),
                ),
                (
                    receipt(&fp, Some("g1"), Dispatched),
                    Err(Refusal::ReconciliationRequired),
                ),
                (
                    receipt(&fp, Some("g1"), UnknownReconcile),
                    Err(Refusal::ReconciliationRequired),
                ),
                (
                    receipt(&fp, Some("g1"), ObservedSuccess),
                    Ok(ReserveOutcome::Replayed(Replay::EffectCompleted)),
                ),
                (
                    receipt(&fp, Some("g1"), ObservedFailure),
                    Ok(ReserveOutcome::Replayed(Replay::EffectFailed)),
                ),
            ];
            for (stored, want) in cases {
                assert_eq!(r.existing(&stored), want, "{stored:?}");
            }
        });
    }

    #[test]
    fn require_approval_without_grant_and_allow_ignores_a_grant() {
        let mut l = MemoryLedger::default();
        let observed = observe(&FixedClock::new(now()), &mut l).unwrap();
        let label = GrantLabel::new("g1").unwrap();
        decided(&publish_request(), |a| {
            assert_eq!(
                ReserveRequest::new(a, observed, None).unwrap_err(),
                Refusal::ApprovalRequired
            );
        });
        decided(&base_request(), |a| {
            let r = ReserveRequest::new(a, observed, Some(&label)).unwrap();
            assert_eq!(r.grant_label(), None, "allow: no effective grant");
            assert_eq!(
                r.check_grant(None),
                GrantCheck::Refuse(Refusal::GrantUnknown)
            );
        });
    }

    #[test]
    fn labels_verdicts_and_statuses() {
        assert!(GrantLabel::new("").is_none());
        assert!(GrantLabel::new(&"x".repeat(129)).is_none());
        assert_eq!(
            GrantLabel::new(&"x".repeat(128)).unwrap().as_str().len(),
            128
        );
        assert_eq!(Verdict::Approved.as_str(), "approved");
        assert_eq!(Verdict::Denied.as_str(), "denied");
        for s in [
            "reserved",
            "dispatched",
            "unknown_reconcile",
            "observed_success",
            "observed_failure",
        ] {
            assert_eq!(ReceiptStatus::parse(s).unwrap().as_str(), s);
        }
        assert_eq!(ReceiptStatus::parse("Reserved"), None);
    }

    struct FixedAuthority(Result<AuthenticatedApprover, ApproverRefusal>);

    impl ApprovalAuthority for FixedAuthority {
        fn authenticate(
            &self,
            _: &ApprovalPresentation<'_>,
        ) -> Result<AuthenticatedApprover, ApproverRefusal> {
            self.0.clone()
        }
    }

    #[test]
    fn authority_answers_are_checked_against_the_presentation() {
        let p = ApprovalPresentation {
            channel: "approval-ui",
            approver: "human:alice",
            credential: b"c",
        };
        let answer = |id: &str, kind, channel: &str| {
            FixedAuthority(Ok(AuthenticatedApprover {
                id: id.into(),
                kind,
                channel: channel.into(),
            }))
        };
        use PrincipalKind::*;
        assert!(authenticate(&answer("human:alice", Human, "approval-ui"), &p).is_ok());
        let cases = [
            (
                answer("human:alice", Human, "other"),
                ApproverRefusal::ChannelMismatch,
            ),
            (
                answer("human:bob", Human, "approval-ui"),
                ApproverRefusal::IdentityMismatch,
            ),
            (
                answer("human:alice", Agent, "approval-ui"),
                ApproverRefusal::NotHuman,
            ),
            (
                answer("human:alice", Model, "approval-ui"),
                ApproverRefusal::NotHuman,
            ),
            (
                answer("human:alice", Service, "approval-ui"),
                ApproverRefusal::NotHuman,
            ),
            (
                FixedAuthority(Err(ApproverRefusal::UntrustedChannel)),
                ApproverRefusal::UntrustedChannel,
            ),
        ];
        for (authority, want) in cases {
            assert_eq!(authenticate(&authority, &p), Err(want));
        }
    }

    #[test]
    fn key_locks_serialize_one_key_and_not_others() {
        let locks = KeyLocks::new();
        let held = locks.lock("t", "k");
        let (tx, rx) = mpsc::channel();
        thread::scope(|s| {
            s.spawn(|| {
                let _other = locks.lock("t", "other");
                tx.send("other").unwrap();
                let _same = locks.lock("t", "k");
                tx.send("same").unwrap();
            });
            assert_eq!(rx.recv_timeout(Duration::from_secs(10)), Ok("other"));
            assert!(
                rx.recv_timeout(Duration::from_millis(200)).is_err(),
                "the same key waits"
            );
            drop(held);
            assert_eq!(rx.recv_timeout(Duration::from_secs(10)), Ok("same"));
        });
        assert_eq!(locks.held(), 0, "entries are removed on release");
    }
}
