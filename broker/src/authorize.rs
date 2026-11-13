//! Use-time authorization (D-008, INV-14, risk 13).
//!
//! The broker never trusts a previously returned Decision: every use re-runs
//! the pure `writ-kernel` decision on the sealed request with the policy that
//! is active now, the trusted state revision and the trusted clock. The policy
//! comes only from a shared hold of the ActivePolicy gate ([`TrustedInputs`] is
//! constructible only from a [`SharedPolicy`]), and the result
//! ([`Authorized`]) is bound to that hold, so authorization and every later
//! use of it happen under one consistent view of the policy (owner decision of
//! 2026-11-12: gate ownership is enforced by the API, not by convention). There is deliberately no API that accepts an earlier
//! Decision. Composite multi-effect requests are refused exactly as the kernel
//! decides (`COMPOSITE_NOT_SUPPORTED`, risk 11, RM-01).
//!
//! Approval grants (`require_approval`) are consumed by M3-03; this module
//! only reports that the use-time decision requires one.

use crate::clock::TrustedClock;
use crate::ingress::VerifiedRequest;
use crate::policy::SharedPolicy;
use crate::refusal::Refusal;
use writ_kernel::contracts::{
    ActionRequest, Decision, DecisionStatus, KernelCode, ReasonCode, StateRevision,
};
use writ_kernel::decision::{self, Policy};

/// Trusted inputs of one use-time decision: the active policy as seen through
/// one shared hold of the ActivePolicy gate, and the trusted state revision.
///
/// The only constructor is [`SharedPolicy::trusted_inputs`], so use-time
/// authorization is impossible without holding the gate shared (owner decision
/// of 2026-11-12). The inputs borrow the guard and cannot outlive it. They
/// cannot be built from a bare policy:
///
/// ```compile_fail,E0451
/// use writ_broker::authorize::TrustedInputs;
/// use writ_kernel::contracts::StateRevision;
/// let _inputs = TrustedInputs {
///     shared: todo!(),
///     state_revision: StateRevision::new(5).unwrap(),
/// };
/// ```
///
/// The supported path goes through the guard:
///
/// ```
/// use writ_broker::policy::{ActivePolicy, PolicyState};
/// use writ_kernel::contracts::StateRevision;
/// use writ_kernel::decision::Policy;
/// let active = ActivePolicy::new(PolicyState::Absent);
/// let shared = active.shared();
/// let inputs = shared.trusted_inputs(StateRevision::new(5).unwrap());
/// assert!(matches!(inputs.policy(), Policy::Absent));
/// ```
#[derive(Clone, Copy, Debug)]
pub struct TrustedInputs<'g> {
    shared: &'g SharedPolicy<'g>,
    state_revision: StateRevision,
}

impl<'g> TrustedInputs<'g> {
    /// Inputs under the shared hold `shared` (crate-private; see
    /// [`SharedPolicy::trusted_inputs`]).
    pub(crate) fn new(shared: &'g SharedPolicy<'g>, state_revision: StateRevision) -> Self {
        TrustedInputs {
            shared,
            state_revision,
        }
    }

    /// The currently active authorization bundle as seen through the hold:
    /// [`Policy::Loaded`], or [`Policy::Absent`] when the policy dependency is
    /// down or an activation was interrupted (the decision is then
    /// `unavailable` / `POLICY_UNAVAILABLE`), or [`Policy::Rejected`].
    pub fn policy(&self) -> Policy<'g> {
        self.shared.policy()
    }

    /// The trusted kernel state revision.
    pub fn state_revision(&self) -> StateRevision {
        self.state_revision
    }
}

/// What the use-time decision allows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UseTimeStatus {
    /// Allowed without approval.
    Allow,
    /// Allowed only with a matching human approval grant (M3-03).
    RequireApproval,
}

/// A sealed request together with the decision made for it at use time: a
/// capability bound to the shared hold of the ActivePolicy gate it was decided
/// under.
///
/// It borrows that hold (`'g`), so it can be used only while the hold lasts and
/// cannot be stored and reused after the gate is released or the policy is
/// replaced. It is not `Clone`. Every consumption API (permit issue now;
/// grant consumption and dispatch in M3-03 and M3-04) takes an `Authorized` or
/// acquires the gate itself ([`crate::policy::ActivePolicy::execute`]).
///
/// It cannot outlive its guard:
///
/// ```compile_fail,E0515
/// use writ_broker::authorize::{authorize, Authorized};
/// use writ_broker::clock::TrustedClock;
/// use writ_broker::ingress::VerifiedRequest;
/// use writ_broker::policy::ActivePolicy;
/// use writ_kernel::contracts::StateRevision;
///
/// fn escape<'a>(
///     active: &'a ActivePolicy,
///     verified: &VerifiedRequest,
///     clock: &dyn TrustedClock,
/// ) -> Option<Authorized<'a>> {
///     let shared = active.shared();
///     let inputs = shared.trusted_inputs(StateRevision::new(5).unwrap());
///     authorize(verified, &inputs, clock).ok()
/// }
/// ```
///
/// The guard cannot be released while the capability is alive:
///
/// ```compile_fail,E0505
/// use writ_broker::authorize::authorize;
/// use writ_broker::clock::TrustedClock;
/// use writ_broker::ingress::VerifiedRequest;
/// use writ_broker::policy::ActivePolicy;
/// use writ_kernel::contracts::StateRevision;
///
/// fn release_early(active: &ActivePolicy, verified: &VerifiedRequest, clock: &dyn TrustedClock) {
///     let shared = active.shared();
///     let inputs = shared.trusted_inputs(StateRevision::new(5).unwrap());
///     let authorized = authorize(verified, &inputs, clock);
///     drop(shared);
///     let _ = authorized.map(|a| a.status());
/// }
/// ```
///
/// Within the hold it is usable (this compiles):
///
/// ```
/// use writ_broker::authorize::authorize;
/// use writ_broker::clock::TrustedClock;
/// use writ_broker::ingress::VerifiedRequest;
/// use writ_broker::policy::ActivePolicy;
/// use writ_kernel::contracts::StateRevision;
///
/// fn within_hold(active: &ActivePolicy, verified: &VerifiedRequest, clock: &dyn TrustedClock) {
///     let shared = active.shared();
///     let inputs = shared.trusted_inputs(StateRevision::new(5).unwrap());
///     let authorized = authorize(verified, &inputs, clock);
///     let _ = authorized.map(|a| a.status());
///     drop(shared);
/// }
/// ```
#[derive(Debug)]
pub struct Authorized<'g> {
    hold: &'g SharedPolicy<'g>,
    verified: VerifiedRequest,
    request: ActionRequest,
    decision: Decision,
    status: UseTimeStatus,
}

impl<'g> Authorized<'g> {
    /// The shared hold of the gate this decision was made under (for checks
    /// that must happen under the same hold, such as permit verification).
    pub fn hold(&self) -> &'g SharedPolicy<'g> {
        self.hold
    }

    /// The sealed request.
    pub fn verified(&self) -> &VerifiedRequest {
        &self.verified
    }

    /// The typed request.
    pub fn request(&self) -> &ActionRequest {
        &self.request
    }

    /// The use-time kernel decision.
    pub fn decision(&self) -> &Decision {
        &self.decision
    }

    /// `allow` or `require_approval`.
    pub fn status(&self) -> UseTimeStatus {
        self.status
    }

    /// Digest of the bundle the decision was made under.
    pub fn policy_bundle_sha(&self) -> &str {
        &self.decision.policy_bundle_sha
    }
}

/// Decide `verified` now, under `inputs` and the time read once from `clock`.
/// Unavailable time is refused as `TIME_UNAVAILABLE` before any decision.
/// Any status other than `allow` or `require_approval` is refused with the
/// decision's reason code. The result is bound to the hold `inputs` came from.
pub fn authorize<'g>(
    verified: &VerifiedRequest,
    inputs: &TrustedInputs<'g>,
    clock: &dyn TrustedClock,
) -> Result<Authorized<'g>, Refusal> {
    let now = clock.now().map_err(|e| e.refusal())?;
    let decision = decision::decide(
        verified.request(),
        inputs.policy(),
        inputs.state_revision,
        now,
    );
    let status = match decision.status {
        DecisionStatus::Allow => UseTimeStatus::Allow,
        DecisionStatus::RequireApproval => UseTimeStatus::RequireApproval,
        status => {
            return Err(Refusal::Decision {
                status,
                reason: decision.reason_code,
            })
        }
    };
    // An allowing decision implies a schema-valid request; fail closed regardless.
    let request = ActionRequest::from_value(verified.request()).map_err(|_| Refusal::Decision {
        status: DecisionStatus::Invalid,
        reason: ReasonCode::Kernel(KernelCode::RequestSchemaInvalid),
    })?;
    Ok(Authorized {
        hold: inputs.shared,
        verified: verified.clone(),
        request,
        decision,
        status,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::clock::FixedClock;
    use crate::policy::{ActivePolicy, PolicyState};
    use crate::test_support::*;
    use writ_kernel::json::{self, Value};

    fn decide_code(req: &Value, state: PolicyState) -> Result<UseTimeStatus, String> {
        let v = verify(req);
        let active = ActivePolicy::new(state);
        let shared = active.shared();
        authorize(
            &v,
            &shared.trusted_inputs(revision(5)),
            &FixedClock::new(now()),
        )
        .map(|a| a.status())
        .map_err(|r| r.code().to_string())
    }

    fn loaded(bundle: &writ_kernel::policy::LoadedBundle) -> PolicyState {
        PolicyState::loaded(bundle.clone())
    }

    #[test]
    fn allow_and_require_approval_are_authorized() {
        let bundle = reference_bundle();
        let req = base_request();
        let v = verify(&req);
        let active = ActivePolicy::new(loaded(&bundle));
        let shared = active.shared();
        let a = authorize(
            &v,
            &shared.trusted_inputs(revision(5)),
            &FixedClock::new(now()),
        )
        .unwrap();
        assert_eq!(a.status(), UseTimeStatus::Allow);
        assert_eq!(a.policy_bundle_sha(), bundle.sha.as_str());
        assert_eq!(a.decision().decided_at, now());
        assert_eq!(a.request().idempotency_key, "idem-1");
        assert_eq!(a.verified(), &v);
        assert!(
            std::ptr::eq(a.hold(), &shared),
            "the capability is bound to the hold it was decided under"
        );
        assert_eq!(
            decide_code(&publish_request(), loaded(&bundle)),
            Ok(UseTimeStatus::RequireApproval)
        );
    }

    #[test]
    fn trusted_inputs_come_from_the_hold() {
        let bundle = reference_bundle();
        let active = ActivePolicy::new(loaded(&bundle));
        let shared = active.shared();
        let inputs = shared.trusted_inputs(revision(9));
        assert!(matches!(inputs.policy(), Policy::Loaded(b) if *b == bundle));
        assert_eq!(inputs.state_revision(), revision(9));
        let absent = ActivePolicy::new(PolicyState::Absent);
        let absent_shared = absent.shared();
        assert!(matches!(
            absent_shared.trusted_inputs(revision(9)).policy(),
            Policy::Absent
        ));
    }

    #[test]
    fn refusals_carry_the_kernel_reason() {
        let bundle = reference_bundle();
        let secrets = with(
            &base_request(),
            &["action", "resource", "canonical_resource_id"],
            json::s("workspace:fixture-tenant/fixture-project/secrets/k"),
        );
        assert_eq!(
            decide_code(&secrets, loaded(&bundle)),
            Err("SECRETS_PATH_FORBIDDEN".into())
        );
        assert_eq!(
            decide_code(&base_request(), PolicyState::Absent),
            Err("POLICY_UNAVAILABLE".into())
        );
        assert_eq!(
            decide_code(&base_request(), PolicyState::Rejected),
            Err("POLICY_BUNDLE_REJECTED".into())
        );
        let stale = with(&base_request(), &["expected_state_revision"], Value::Int(4));
        assert_eq!(
            decide_code(&stale, loaded(&bundle)),
            Err("STATE_REVISION_CONFLICT".into())
        );
        let malformed = without(&base_request(), &["trace_id"]);
        assert_eq!(
            decide_code(&malformed, loaded(&bundle)),
            Err("REQUEST_SCHEMA_INVALID".into())
        );
        let past = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T12:00:00Z"),
        );
        assert_eq!(
            decide_code(&past, loaded(&bundle)),
            Err("DEADLINE_EXCEEDED".into())
        );
    }

    #[test]
    fn composite_multi_effect_requests_are_refused_as_the_kernel_says() {
        // Risk 11 / RM-01: a promotion combined with another effect.
        let active = ActivePolicy::new(PolicyState::loaded(transition_bundle()));
        let shared = active.shared();
        let inputs = shared.trusted_inputs(revision(7));
        let base = transition_request();
        let single = authorize(&verify(&base), &inputs, &FixedClock::new(transition_now()));
        assert_eq!(
            single.map(|a| a.status()),
            Ok(UseTimeStatus::Allow),
            "the single-effect transition is allowed"
        );
        for extra in ["repository.publish", "process.execute", "file.write"] {
            let composite = with(
                &base,
                &["action", "requested_capabilities"],
                Value::Arr(vec![json::s(extra)]),
            );
            let v = verify(&composite);
            let refusal = authorize(&v, &inputs, &FixedClock::new(transition_now())).unwrap_err();
            assert_eq!(
                refusal,
                Refusal::Decision {
                    status: DecisionStatus::Invalid,
                    reason: ReasonCode::Kernel(KernelCode::CompositeNotSupported),
                },
                "{extra}"
            );
            assert_eq!(refusal.external_code(), "COMPOSITE_NOT_SUPPORTED");
        }
    }

    #[test]
    fn swapped_sub_effect_of_a_composite_is_refused_at_ingress_and_by_the_kernel() {
        let composite = with(
            &transition_request(),
            &["action", "requested_capabilities"],
            Value::Arr(vec![json::s("process.execute")]),
        );
        let seal = sealed(&composite, FIXTURE_ADAPTER, &FIXTURE_KEY);
        // Swapping the sub-effect after sealing breaks the seal.
        let swapped = with(
            &composite,
            &["action", "requested_capabilities"],
            Value::Arr(vec![json::s("repository.publish")]),
        );
        assert_eq!(
            crate::ingress::verify_ingress(&swapped, Some(&seal), &fixture_ring()),
            Err(Refusal::IngressUnauthenticated)
        );
        // Sealed with the swapped sub-effect, it is still a composite and refused.
        let v = verify(&swapped);
        let active = ActivePolicy::new(PolicyState::loaded(transition_bundle()));
        let shared = active.shared();
        let refusal = authorize(
            &v,
            &shared.trusted_inputs(revision(7)),
            &FixedClock::new(transition_now()),
        )
        .unwrap_err();
        assert_eq!(refusal.code(), "COMPOSITE_NOT_SUPPORTED");
    }

    #[test]
    fn decision_uses_the_supplied_clock_and_revision() {
        let req = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T12:00:10Z"),
        );
        let v = verify(&req);
        let active = ActivePolicy::new(PolicyState::loaded(reference_bundle()));
        let shared = active.shared();
        let inputs = shared.trusted_inputs(revision(5));
        let mut clock = FixedClock::new(now());
        assert!(authorize(&v, &inputs, &clock).is_ok());
        clock.advance(10);
        assert_eq!(
            authorize(&v, &inputs, &clock).unwrap_err().code(),
            "DEADLINE_EXCEEDED"
        );
        let pinned = with(&base_request(), &["expected_state_revision"], Value::Int(5));
        let v = verify(&pinned);
        let clock = FixedClock::new(now());
        assert!(authorize(&v, &inputs, &clock).is_ok());
        let moved = shared.trusted_inputs(revision(6));
        assert_eq!(
            authorize(&v, &moved, &clock).unwrap_err().code(),
            "STATE_REVISION_CONFLICT"
        );
    }
}
