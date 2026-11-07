//! Agent-facing refusal mapping (D-018 A7, D-019). Internal codes go to the
//! audit record; an agent or host receives only this mapping. Codes that
//! describe only the caller's own input, key or expected revision pass
//! through; every other acceptance, permit, scope or trusted-state code, and
//! every unknown code, becomes `PROMOTION_REFUSED` (no disclosure of
//! cross-tenant existence, policy predicates, evaluator identities or secrets).

const PASS_THROUGH: [&str; 14] = [
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
    // The caller's own expected revision.
    "REVISION_CONFLICT",
    "STATE_REVISION_CONFLICT",
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
