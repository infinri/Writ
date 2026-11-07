package broker

import (
	"encoding/hex"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

// D-020 restart_broker: the ledger persists, client-held permits remain
// presentable, volatile state is rebuilt.

const (
	rtDigest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	rtKeyHex = "00112233445566778899aabbccddeeff"
)

func rtParse(t *testing.T, s string) map[string]any {
	t.Helper()
	v, err := sjson.Parse(s)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return v.(map[string]any)
}

func rtBroker(t *testing.T) *Broker {
	return newBroker(rtParse(t, `{"now":"2026-11-07T11:00:00Z","state_revision":7,
		"ingress_keys":{"iss":"`+rtKeyHex+`"},"permit_key":"ffeeddccbbaa99887766554433221100",
		"bundle":{"contract_version":"2.0","bundle_id":"b","version":"1","publisher":"p",
		"known_capabilities":["file.write"],"trusted_principal_issuers":["iss"],
		"statements":[{"statement_id":"permit-write","effect":"permit","action_kinds":["file.write"],"reason_code":"WRITE_OK"}]}}`))
}

func rtExecute(t *testing.T, key, fault string) map[string]any {
	req := rtParse(t, `{"contract_version":"2.0","request_id":"r-`+key+`","trace_id":"t1","session_id":"s1",
		"parent_session_id":null,"agent_id":null,"tenant_id":"ten","project_id":"proj",
		"idempotency_key":"`+key+`","deadline_at":null,"policy_bundle_sha":null,"expected_state_revision":7,
		"principal":{"id":"agent:a","roles":["agent"],"authn_method":"os_peer_credential","verified_by":"iss"},
		"action":{"kind":"file.write","action_digest":"`+rtDigest+`","requested_capabilities":[],
		"resource":{"canonical_resource_id":"workspace:ten/proj/a.txt","uri":"x://y"},"attributes":{}}}`)
	k, _ := hex.DecodeString(rtKeyHex)
	op := map[string]any{"op": "execute", "request": req,
		"seal": map[string]any{"adapter_id": "iss", "mac": mac(k, sjson.MustCanonical(req))}}
	if fault != "" {
		op["fault"] = fault
	}
	return op
}

func rtWant(t *testing.T, got map[string]any, want string) {
	t.Helper()
	if g := string(sjson.Encode(got)); g != want {
		t.Fatalf("got %s, want %s", g, want)
	}
}

func TestRestartKeepsLedger(t *testing.T) {
	b := rtBroker(t)
	first := rtExecute(t, "k1", "")
	rtWant(t, b.apply(first, 0), `{"result":"effect_completed"}`)
	rtWant(t, b.apply(rtExecute(t, "k2", "crash_after_dispatch"), 1), `{"result":"crashed"}`)
	rtWant(t, b.apply(map[string]any{"op": "restart_broker"}, 2), `{"result":"restarted"}`)
	if b.policy == nil || b.policy.Bundle == nil {
		t.Fatal("policy must be reloaded after restart")
	}
	// Consumed permit ids persist; the client-held permit is still presentable.
	rtWant(t, b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"]}, 3),
		`{"code":"PERMIT_REPLAYED","result":"refused"}`)
	// Receipts persist: a retry replays, an uncertain effect stays uncertain.
	rtWant(t, b.apply(rtExecute(t, "k1", ""), 4), `{"replayed":true,"result":"effect_completed"}`)
	rtWant(t, b.apply(rtExecute(t, "k2", ""), 5), `{"code":"RECONCILIATION_REQUIRED","result":"refused"}`)
	// The permit sequence persists, so a new permit never reuses a consumed id.
	// (crash_after_dispatch applied its effect before the crash, as in b-46.)
	rtWant(t, b.apply(rtExecute(t, "k3", ""), 6), `{"result":"effect_completed"}`)
	rtWant(t, b.final(), `{"consumed_permits":3,"effects_applied":3,"grant_uses":{},"receipts":{"dispatched":1,"observed_success":2},"refusals":2}`)
}
