// Package refusal maps internal refusal codes to the agent-facing response
// (D-018 A7, D-019). Codes that describe only the caller's own input,
// idempotency key or expected revision pass through; every other code,
// including unknown ones, becomes PROMOTION_REFUSED.
package refusal

const Generic = "PROMOTION_REFUSED"

var passThrough = map[string]bool{
	"INPUT_JSON_INVALID":             true,
	"REQUEST_JSON_INVALID":           true,
	"ACCEPTANCE_RECORD_MALFORMED":    true,
	"TRANSITION_MALFORMED":           true,
	"WORKFLOW_RESOURCE_INVALID":      true,
	"REQUESTED_CAPABILITIES_INVALID": true,
	"COMPOSITE_NOT_SUPPORTED":        true,
	"IDEMPOTENCY_CONFLICT":           true,
	"REVISION_CONFLICT":              true,
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
