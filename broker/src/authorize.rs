//! Use-time authorization (D-008, INV-14, risk 13).
//!
//! The broker never trusts a previously returned Decision: every use re-runs
//! the pure `writ-kernel` decision on the sealed request with the policy that
//! is active now, the trusted state revision and the trusted clock, all
//! supplied explicitly. There is deliberately no API that accepts an earlier
//! Decision. Composite multi-effect requests are refused exactly as the kernel
//! decides (`COMPOSITE_NOT_SUPPORTED`, risk 11, RM-01).
//!
//! Approval grants (`require_approval`) are consumed by M3-03; this module
//! only reports that the use-time decision requires one.

use crate::clock::TrustedClock;
use crate::ingress::VerifiedRequest;
use crate::refusal::Refusal;
use writ_kernel::contracts::{
    ActionRequest, Decision, DecisionStatus, KernelCode, ReasonCode, StateRevision,
};
use writ_kernel::decision::{self, Policy};

/// Trusted inputs of one use-time decision, supplied by the broker's owner.
#[derive(Clone, Copy, Debug)]
pub struct TrustedInputs<'a> {
    /// The currently active authorization bundle: [`Policy::Loaded`], or
    /// [`Policy::Absent`] when the policy dependency is down (the decision is
    /// then `unavailable` / `POLICY_UNAVAILABLE`), or [`Policy::Rejected`].
    pub policy: Policy<'a>,
    /// The trusted kernel state revision.
    pub state_revision: StateRevision,
}

/// What the use-time decision allows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UseTimeStatus {
    /// Allowed without approval.
    Allow,
    /// Allowed only with a matching human approval grant (M3-03).
    RequireApproval,
}

/// A sealed request together with the decision made for it at use time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Authorized {
    verified: VerifiedRequest,
    request: ActionRequest,
    decision: Decision,
    status: UseTimeStatus,
}

impl Authorized {
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
/// Any status other than `allow` or `require_approval` is refused with the
/// decision's reason code.
pub fn authorize(
    verified: &VerifiedRequest,
    inputs: &TrustedInputs<'_>,
    clock: &dyn TrustedClock,
) -> Result<Authorized, Refusal> {
    let decision = decision::decide(
        verified.request(),
        inputs.policy,
        inputs.state_revision,
        clock.now(),
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
    use crate::test_support::*;
    use writ_kernel::json::{self, Value};

    fn decide_code(req: &Value, policy: Policy<'_>) -> Result<UseTimeStatus, String> {
        let v = verify(req);
        let inputs = TrustedInputs {
            policy,
            state_revision: revision(5),
        };
        authorize(&v, &inputs, &FixedClock::new(now()))
            .map(|a| a.status())
            .map_err(|r| r.code().to_string())
    }

    #[test]
    fn allow_and_require_approval_are_authorized() {
        let bundle = reference_bundle();
        let req = base_request();
        let v = verify(&req);
        let a = authorize(
            &v,
            &TrustedInputs {
                policy: Policy::Loaded(&bundle),
                state_revision: revision(5),
            },
            &FixedClock::new(now()),
        )
        .unwrap();
        assert_eq!(a.status(), UseTimeStatus::Allow);
        assert_eq!(a.policy_bundle_sha(), bundle.sha.as_str());
        assert_eq!(a.decision().decided_at, now());
        assert_eq!(a.request().idempotency_key, "idem-1");
        assert_eq!(a.verified(), &v);
        assert_eq!(
            decide_code(&publish_request(), Policy::Loaded(&bundle)),
            Ok(UseTimeStatus::RequireApproval)
        );
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
            decide_code(&secrets, Policy::Loaded(&bundle)),
            Err("SECRETS_PATH_FORBIDDEN".into())
        );
        assert_eq!(
            decide_code(&base_request(), Policy::Absent),
            Err("POLICY_UNAVAILABLE".into())
        );
        assert_eq!(
            decide_code(&base_request(), Policy::Rejected),
            Err("POLICY_BUNDLE_REJECTED".into())
        );
        let stale = with(&base_request(), &["expected_state_revision"], Value::Int(4));
        assert_eq!(
            decide_code(&stale, Policy::Loaded(&bundle)),
            Err("STATE_REVISION_CONFLICT".into())
        );
        let malformed = without(&base_request(), &["trace_id"]);
        assert_eq!(
            decide_code(&malformed, Policy::Loaded(&bundle)),
            Err("REQUEST_SCHEMA_INVALID".into())
        );
        let past = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T12:00:00Z"),
        );
        assert_eq!(
            decide_code(&past, Policy::Loaded(&bundle)),
            Err("DEADLINE_EXCEEDED".into())
        );
    }

    #[test]
    fn composite_multi_effect_requests_are_refused_as_the_kernel_says() {
        // Risk 11 / RM-01: a promotion combined with another effect.
        let bundle = transition_bundle();
        let base = transition_request();
        let single = authorize(
            &verify(&base),
            &TrustedInputs {
                policy: Policy::Loaded(&bundle),
                state_revision: revision(7),
            },
            &FixedClock::new(transition_now()),
        );
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
            let refusal = authorize(
                &v,
                &TrustedInputs {
                    policy: Policy::Loaded(&bundle),
                    state_revision: revision(7),
                },
                &FixedClock::new(transition_now()),
            )
            .unwrap_err();
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
        let bundle = transition_bundle();
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
        let refusal = authorize(
            &v,
            &TrustedInputs {
                policy: Policy::Loaded(&bundle),
                state_revision: revision(7),
            },
            &FixedClock::new(transition_now()),
        )
        .unwrap_err();
        assert_eq!(refusal.code(), "COMPOSITE_NOT_SUPPORTED");
    }

    #[test]
    fn decision_uses_the_supplied_clock_and_revision() {
        let bundle = reference_bundle();
        let req = with(
            &base_request(),
            &["deadline_at"],
            json::s("2026-11-05T12:00:10Z"),
        );
        let v = verify(&req);
        let inputs = TrustedInputs {
            policy: Policy::Loaded(&bundle),
            state_revision: revision(5),
        };
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
        let moved = TrustedInputs {
            state_revision: revision(6),
            ..inputs
        };
        assert_eq!(
            authorize(&v, &moved, &clock).unwrap_err().code(),
            "STATE_REVISION_CONFLICT"
        );
    }
}
