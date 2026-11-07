package kernel

import (
	"testing"
	"time"

	"writ.dev/m1/proto/internal/sjson"
)

// D-020: transition support versus malformed protection.

const testDigest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

func parse(t *testing.T, s string) map[string]any {
	t.Helper()
	v, err := sjson.Parse(s)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return v.(map[string]any)
}

// testBundle builds a bundle; extra is spliced into the top-level object and
// stmts into the statement list.
func testBundle(t *testing.T, known, stmts, extra string) map[string]any {
	return parse(t, `{"contract_version":"2.0","bundle_id":"b","version":"1","publisher":"p",
		"known_capabilities":[`+known+`],"trusted_principal_issuers":["iss"],
		"statements":[{"statement_id":"permit-write","effect":"permit","action_kinds":["file.write"],
		"reason_code":"WRITE_OK"}`+stmts+`]`+extra+`}`)
}

func testRequest(t *testing.T, kind, rid, attrs string) map[string]any {
	return parse(t, `{"contract_version":"2.0","request_id":"r1","trace_id":"t1","session_id":"s1",
		"parent_session_id":null,"agent_id":null,"tenant_id":"ten","project_id":"proj",
		"idempotency_key":"k1","deadline_at":null,"policy_bundle_sha":null,"expected_state_revision":7,
		"principal":{"id":"agent:a","roles":["agent"],"authn_method":"os_peer_credential","verified_by":"iss"},
		"action":{"kind":"`+kind+`","action_digest":"`+testDigest+`","requested_capabilities":[],
		"resource":{"canonical_resource_id":"`+rid+`","uri":"x://y"},"attributes":`+attrs+`}}`)
}

const boundAttrs = `{"from_state":"verifying","to_state":"accepted","acceptance_record_id":"acc-1",
	"artifact_snapshot_id":"snap-1","artifact_digest":"` + testDigest + `"}`

var testNow = time.Date(2026, 11, 7, 11, 0, 0, 0, time.UTC)

func decideWith(t *testing.T, bundle, req map[string]any) *Decision {
	t.Helper()
	return Decide(req, LoadPolicy(bundle), 7, testNow)
}

func expect(t *testing.T, d *Decision, status, reason string) {
	t.Helper()
	if d.Status != status || d.ReasonCode != reason {
		t.Fatalf("got %s/%s, want %s/%s", d.Status, d.ReasonCode, status, reason)
	}
}

const wfID = "workflow:ten/proj/wf-1"

func TestKnownButUnsupportedTransition(t *testing.T) {
	b := testBundle(t, `"file.write","workflow.transition"`, ``, ``)
	if lb := LoadBundle(b); lb == nil || lb.TransitionSupported {
		t.Fatal("known-only bundle must load without transition support")
	}
	expect(t, decideWith(t, b, testRequest(t, TransitionKind, wfID, boundAttrs)), "deny", "UNSUPPORTED_CAPABILITY")
	// Support is checked before resource shape and attributes.
	expect(t, decideWith(t, b, testRequest(t, TransitionKind, "workflow:ten/proj/wf-1/extra", `{}`)), "deny", "UNSUPPORTED_CAPABILITY")
	// Scope is checked before support.
	expect(t, decideWith(t, b, testRequest(t, TransitionKind, "workflow:other/proj/wf-1", boundAttrs)), "deny", "RESOURCE_SCOPE_MISMATCH")
	// Other capabilities stay usable.
	expect(t, decideWith(t, b, testRequest(t, "file.write", "workspace:ten/proj/a.txt", `{}`)), "allow", "WRITE_OK")
}

func TestUnknownTransitionCapability(t *testing.T) {
	b := testBundle(t, `"file.write"`, ``, ``)
	expect(t, decideWith(t, b, testRequest(t, TransitionKind, wfID, boundAttrs)), "deny", "UNKNOWN_CAPABILITY")
	expect(t, decideWith(t, b, testRequest(t, "file.write", "workspace:ten/proj/a.txt", `{}`)), "allow", "WRITE_OK")
}

func TestDeclaredSupportRequiresValidProtection(t *testing.T) {
	stmt := `,{"statement_id":"permit-tx","effect":"permit","action_kinds":["workflow.transition"],
		"reason_code":"TX_OK","to_states":["review"]}`
	cases := map[string]map[string]any{
		"statement without protection": testBundle(t, `"file.write","workflow.transition"`, stmt, ``),
		"protection missing binding": testBundle(t, `"file.write","workflow.transition"`, ``,
			`,"workflow_protection":{"protected_to_states":["accepted"],"protected_transitions":[]}`),
		"protection malformed digest": testBundle(t, `"file.write","workflow.transition"`, ``,
			`,"workflow_protection":{"acceptance_policy_bundle_sha":"nope","protected_to_states":["accepted"],"protected_transitions":[]}`),
	}
	for name, b := range cases {
		if LoadBundle(b) != nil {
			t.Fatalf("%s: bundle must be rejected", name)
		}
		expect(t, decideWith(t, b, testRequest(t, TransitionKind, wfID, boundAttrs)), "unavailable", "POLICY_BUNDLE_REJECTED")
	}
}

func TestValidProtectionWithoutRuleIsDefaultDeny(t *testing.T) {
	b := testBundle(t, `"file.write","workflow.transition"`, ``,
		`,"workflow_protection":{"acceptance_policy_bundle_sha":"`+testDigest+`","protected_to_states":["accepted"],"protected_transitions":[]}`)
	lb := LoadBundle(b)
	if lb == nil || !lb.TransitionSupported {
		t.Fatal("protection alone declares support")
	}
	expect(t, decideWith(t, b, testRequest(t, TransitionKind, wfID, boundAttrs)), "deny", "DEFAULT_DENY")
}
