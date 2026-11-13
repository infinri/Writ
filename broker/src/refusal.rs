//! Broker refusals of the decision-free core (M3-02).
//!
//! A refusal carries the internal code that goes to the access-controlled
//! audit record. Agents and hosts receive only [`Refusal::external_code`], the
//! kernel's agent-facing mapping (D-018 A7, D-020).

use std::fmt;
use writ_kernel::contracts::{DecisionStatus, ReasonCode};
use writ_kernel::refusal::external_code;

/// Why the broker refused a request or a permit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The seal is absent, malformed, forged, made by an unregistered adapter
    /// or another adapter than `principal.verified_by`, or the request was
    /// altered after sealing (D-010).
    IngressUnauthenticated,
    /// The use-time kernel decision is neither `allow` nor `require_approval`;
    /// the code is the decision's `reason_code`.
    Decision {
        /// Decision status.
        status: DecisionStatus,
        /// Decision reason (kernel code or the deciding statement's code).
        reason: ReasonCode,
    },
    /// The permit's request fingerprint differs from the presented request (D-012).
    PermitRequestMismatch,
    /// The permit is malformed or its MAC does not verify under the permit key.
    PermitInvalid,
    /// The trusted time is at or after the permit's expiry.
    PermitExpired,
    /// The permit is bound to a policy bundle digest other than the currently
    /// active authorization bundle, or no verified bundle is active (D-018 A3,
    /// owner decision A of 2026-11-12). Carries no digest: the refusal never
    /// reveals the active bundle.
    PermitStale,
    /// Trusted time could not be established: a clock-source failure, an
    /// overflow, an unacknowledged startup rollback or a persisted-floor
    /// failure ([`crate::clock::ClockError`]). Fail closed; externally the
    /// kernel's generic mapping.
    TimeUnavailable,
}

impl Refusal {
    /// Internal code (audit record and test harnesses only).
    pub fn code(&self) -> &str {
        match self {
            Refusal::IngressUnauthenticated => "INGRESS_UNAUTHENTICATED",
            Refusal::Decision { reason, .. } => reason.as_str(),
            Refusal::PermitRequestMismatch => "PERMIT_REQUEST_MISMATCH",
            Refusal::PermitInvalid => "PERMIT_INVALID",
            Refusal::PermitExpired => "PERMIT_EXPIRED",
            Refusal::PermitStale => "PERMIT_STALE",
            Refusal::TimeUnavailable => "TIME_UNAVAILABLE",
        }
    }

    /// Agent-facing code (the kernel's external refusal mapping).
    pub fn external_code(&self) -> &'static str {
        external_code(self.code())
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for Refusal {}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use writ_kernel::contracts::KernelCode;

    #[test]
    fn codes_and_external_mapping() {
        let cases = [
            (
                Refusal::IngressUnauthenticated,
                "INGRESS_UNAUTHENTICATED",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitRequestMismatch,
                "PERMIT_REQUEST_MISMATCH",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitInvalid,
                "PERMIT_INVALID",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::PermitExpired,
                "PERMIT_EXPIRED",
                "PROMOTION_REFUSED",
            ),
            (Refusal::PermitStale, "PERMIT_STALE", "PROMOTION_REFUSED"),
            (
                Refusal::TimeUnavailable,
                "TIME_UNAVAILABLE",
                "PROMOTION_REFUSED",
            ),
            (
                Refusal::Decision {
                    status: DecisionStatus::Invalid,
                    reason: ReasonCode::Kernel(KernelCode::CompositeNotSupported),
                },
                "COMPOSITE_NOT_SUPPORTED",
                "COMPOSITE_NOT_SUPPORTED",
            ),
            (
                Refusal::Decision {
                    status: DecisionStatus::Deny,
                    reason: ReasonCode::Policy("SECRETS_PATH_FORBIDDEN".into()),
                },
                "SECRETS_PATH_FORBIDDEN",
                "PROMOTION_REFUSED",
            ),
        ];
        for (refusal, internal, external) in cases {
            assert_eq!(refusal.code(), internal);
            assert_eq!(refusal.to_string(), internal);
            assert_eq!(refusal.external_code(), external, "{internal}");
        }
    }

    #[test]
    fn permit_stale_uses_the_kernel_generic_mapping() {
        // Not a pass-through code of the kernel: the generic external refusal.
        assert!(!writ_kernel::refusal::PASS_THROUGH.contains(&"PERMIT_STALE"));
        assert_eq!(external_code("PERMIT_STALE"), "PROMOTION_REFUSED");
        assert_eq!(
            Refusal::PermitStale.external_code(),
            writ_kernel::refusal::PROMOTION_REFUSED
        );
    }

    #[test]
    fn time_unavailable_uses_the_kernel_generic_mapping() {
        assert!(!writ_kernel::refusal::PASS_THROUGH.contains(&"TIME_UNAVAILABLE"));
        assert_eq!(
            Refusal::TimeUnavailable.external_code(),
            writ_kernel::refusal::PROMOTION_REFUSED
        );
    }
}
