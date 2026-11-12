//! Agent-facing refusal mapping (D-018 A7, D-019, D-020).
//!
//! Internal codes go to the access-controlled audit record; an agent or host
//! receives only this mapping. Codes that describe only the caller's own
//! input, request or idempotency key pass through (the list in
//! `m1/fixtures/acceptance/SEMANTICS.md`), plus `APPROVAL_REQUIRED`, which
//! tells the caller to obtain a human approval through the trusted channel.
//! Every other code, including trusted-state revision conflicts (D-020) and
//! every unknown code, becomes [`PROMOTION_REFUSED`]: no disclosure of
//! protected state, cross-tenant existence, policy predicates, evaluator
//! identities or secrets.

/// The generic external refusal.
pub const PROMOTION_REFUSED: &str = "PROMOTION_REFUSED";

/// Internal codes that pass through unchanged.
pub const PASS_THROUGH: [&str; 12] = [
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

/// The external code for an internal code (exact, case-sensitive match).
pub fn external_code(internal: &str) -> &'static str {
    PASS_THROUGH
        .iter()
        .copied()
        .find(|c| *c == internal)
        .unwrap_or(PROMOTION_REFUSED)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn caller_input_codes_pass_through() {
        for code in PASS_THROUGH {
            assert_eq!(external_code(code), code);
        }
    }

    #[test]
    fn state_and_disclosing_codes_are_refused() {
        for code in [
            "REVISION_CONFLICT",
            "STATE_REVISION_CONFLICT",
            "RESOURCE_SCOPE_MISMATCH",
            "PRINCIPAL_UNVERIFIED",
            "UNSUPPORTED_CAPABILITY",
            "UNKNOWN_CAPABILITY",
            "ACCEPTANCE_BINDING_REQUIRED",
            "ACCEPTANCE_EVALUATOR_REVOKED",
            "NOT_A_CODE",
            "",
            "request_schema_invalid",
            " REQUEST_SCHEMA_INVALID",
        ] {
            assert_eq!(external_code(code), PROMOTION_REFUSED, "{code:?}");
        }
    }
}
