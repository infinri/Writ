// Package refusal maps internal refusal codes to the agent-facing response
// (D-018 A7, D-019, D-020). Codes that describe only the caller's own input
// or idempotency key pass through (the list in acceptance SEMANTICS.md,
// "Internal codes versus external responses"); trusted-state conflicts
// (REVISION_CONFLICT, STATE_REVISION_CONFLICT) and every other code,
// including unknown ones, become PROMOTION_REFUSED.
package refusal

const Generic = "PROMOTION_REFUSED"

var passThrough = map[string]bool{
	"INPUT_JSON_INVALID":             true,
	"REQUEST_JSON_INVALID":           true,
	"REQUEST_SCHEMA_INVALID":         true,
	"DEADLINE_EXCEEDED":              true,
	"ACCEPTANCE_RECORD_MALFORMED":    true,
	"TRANSITION_MALFORMED":           true,
	"WORKFLOW_RESOURCE_INVALID":      true,
	"REQUESTED_CAPABILITIES_INVALID": true,
	"COMPOSITE_NOT_SUPPORTED":        true,
	"IDEMPOTENCY_CONFLICT":           true,
	"RECONCILIATION_REQUIRED":        true,
	"APPROVAL_REQUIRED":              true,
}

// External returns the agent-facing code for an internal code.
func External(internal string) string {
	if passThrough[internal] {
		return internal
	}
	return Generic
}
