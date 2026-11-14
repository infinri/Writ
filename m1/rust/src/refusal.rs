//! Agent-facing refusal mapping (D-018 A7, D-019). Internal codes go to the
//! audit record; an agent or host receives only this mapping. Codes that
//! describe only the caller's own input or idempotency key pass through
//! (exactly the list in m1/fixtures/acceptance/SEMANTICS.md); every other
//! acceptance, permit, scope or trusted-state code (including the revision
//! conflicts, D-020), and every unknown code, becomes `PROMOTION_REFUSED` (no
//! disclosure of protected state, cross-tenant existence, policy predicates,
//! evaluator identities or secrets).

const PASS_THROUGH: [&str; 12] = [
    // Strict parsing and schema of the caller's own input.
    "INPUT_JSON_INVALID",
    "REQUEST_JSON_INVALID",
    "REQUEST_SCHEMA_INVALID",
    "ACCEPTANCE_RECORD_MALFORMED",
    "TRANSITION_MALFORMED",
    "WORKFLOW_RESOURCE_INVALID",
    "REQUESTED_CAPABILITIES_INVALID",
    "COMPOSITE_NOT_SUPPORTED",
    "DEADLINE_EXCEEDED",
    // The caller's own idempotency key.
    "IDEMPOTENCY_CONFLICT",
    "RECONCILIATION_REQUIRED",
    // The caller must obtain a human approval through the trusted channel.
    "APPROVAL_REQUIRED",
];

pub fn external(internal: &str) -> &str {
    if PASS_THROUGH.contains(&internal) {
        internal
    } else {
        "PROMOTION_REFUSED"
    }
}

#[cfg(test)]
mod tests {
    use super::external;

    #[test]
    fn d020_request_errors_pass_through() {
        assert_eq!(external("REQUEST_SCHEMA_INVALID"), "REQUEST_SCHEMA_INVALID");
        assert_eq!(external("DEADLINE_EXCEEDED"), "DEADLINE_EXCEEDED");
    }

    #[test]
    fn d020_revision_conflicts_are_refused() {
        assert_eq!(external("REVISION_CONFLICT"), "PROMOTION_REFUSED");
        assert_eq!(external("STATE_REVISION_CONFLICT"), "PROMOTION_REFUSED");
    }

    #[test]
    fn d020_unknown_and_disclosing_codes_are_refused() {
        for c in ["NOT_A_CODE", "", "RESOURCE_SCOPE_MISMATCH", "PRINCIPAL_UNVERIFIED", "UNSUPPORTED_CAPABILITY", "UNKNOWN_CAPABILITY"] {
            assert_eq!(external(c), "PROMOTION_REFUSED");
        }
    }
}
