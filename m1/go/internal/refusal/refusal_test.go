package refusal

import "testing"

// D-020 mapping: request errors pass through; trusted-state conflicts and
// unknown codes become PROMOTION_REFUSED.
func TestExternalMapping(t *testing.T) {
	cases := map[string]string{
		"REQUEST_SCHEMA_INVALID":  "REQUEST_SCHEMA_INVALID",
		"DEADLINE_EXCEEDED":       "DEADLINE_EXCEEDED",
		"IDEMPOTENCY_CONFLICT":    "IDEMPOTENCY_CONFLICT",
		"REVISION_CONFLICT":       Generic,
		"STATE_REVISION_CONFLICT": Generic,
		"UNSUPPORTED_CAPABILITY":  Generic,
		"UNKNOWN_CAPABILITY":      Generic,
		"RESOURCE_SCOPE_MISMATCH": Generic,
		"NO_SUCH_CODE":            Generic,
		"":                        Generic,
	}
	for in, want := range cases {
		if got := External(in); got != want {
			t.Errorf("External(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPassThroughListIsExact(t *testing.T) {
	want := []string{"INPUT_JSON_INVALID", "REQUEST_JSON_INVALID", "REQUEST_SCHEMA_INVALID", "DEADLINE_EXCEEDED",
		"ACCEPTANCE_RECORD_MALFORMED", "TRANSITION_MALFORMED", "WORKFLOW_RESOURCE_INVALID",
		"REQUESTED_CAPABILITIES_INVALID", "COMPOSITE_NOT_SUPPORTED", "IDEMPOTENCY_CONFLICT",
		"RECONCILIATION_REQUIRED", "APPROVAL_REQUIRED"}
	if len(passThrough) != len(want) {
		t.Fatalf("pass-through has %d codes, want %d", len(passThrough), len(want))
	}
	for _, c := range want {
		if !passThrough[c] {
			t.Errorf("%s missing from pass-through", c)
		}
	}
}
