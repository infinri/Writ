// Package broker runs `broker-scenario` inputs (m1/fixtures/broker/BROKER.md,
// m1-draft-3): sealed ingress, use-time kernel decisions, approval grants,
// permits, idempotent receipts, crashes and reconciliation.
//
// SIMULATED, not verified:
//   - Ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in
//     scenario state (no key management, rotation or registration).
//   - The ledger (grants, receipts, permits) is an in-memory model guarded by
//     one mutex; every operation is one serializable transaction. "fault"
//     injects a crash at a named point; nothing is durable.
//   - An "authenticated human" is a credential match on a fixture channel.
//   - Effect "dispatch" increments a counter; no real side effect occurs.
package broker

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"sync"
	"time"

	"writ.dev/m1/proto/internal/kernel"
	"writ.dev/m1/proto/internal/sjson"
)

const (
	permitTTL       = 30 * time.Second
	defaultGrantTTL = 600
	timeLayout      = "2006-01-02T15:04:05Z"
)

type grant struct {
	verdict string
	binding map[string]any // approval request id, tenant, principal, kind, resource, digest, capability set, bundle sha
	expires time.Time
	maxUses int
	uses    int
}

type receipt struct {
	fingerprint string
	grantLabel  string // "" for allow
	status      string // reserved | dispatched | observed_success | observed_failure | unknown_reconcile
}

type permit struct {
	id          string
	fingerprint string
	bundleSha   string
	expires     time.Time
	mac         string
}

// Broker is the simulated broker and ledger for one scenario.
type Broker struct {
	mu sync.Mutex

	now           time.Time
	stateRevision int64
	bundleDoc     any            // active bundle document (persisted configuration)
	policy        *kernel.Policy // loaded active bundle (volatile cache, reloaded on restart)
	ingressKeys   map[string][]byte
	permitKey     []byte
	channels      map[string]map[string]string
	deps          map[string]string

	grants         map[string]*grant
	receipts       map[string]*receipt // tenant + "\x00" + idempotency key
	permits        map[int]*permit     // by top-level step index
	consumed       map[string]bool
	permitSeq      int
	effectsApplied int
	refusals       int
}

func hexKey(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil
	}
	return b
}

func newBroker(initial map[string]any) *Broker {
	b := &Broker{
		ingressKeys: map[string][]byte{}, channels: map[string]map[string]string{}, deps: map[string]string{},
		grants: map[string]*grant{}, receipts: map[string]*receipt{}, permits: map[int]*permit{}, consumed: map[string]bool{},
	}
	b.now, _ = kernel.ParseTime(sjson.StrOr(initial, "now"))
	b.stateRevision, _ = sjson.Int(initial, "state_revision")
	b.bundleDoc = initial["bundle"]
	b.policy = kernel.LoadPolicy(b.bundleDoc)
	if keys, ok := sjson.Obj(initial["ingress_keys"]); ok {
		for id, v := range keys {
			if s, ok := v.(string); ok {
				if k := hexKey(s); k != nil {
					b.ingressKeys[id] = k
				}
			}
		}
	}
	b.permitKey = hexKey(sjson.StrOr(initial, "permit_key"))
	if ch, ok := sjson.Obj(initial["channels"]); ok {
		for name, v := range ch {
			creds := map[string]string{}
			if m, ok := sjson.Obj(v); ok {
				for who, c := range m {
					if s, ok := c.(string); ok {
						creds[who] = s
					}
				}
			}
			b.channels[name] = creds
		}
	}
	b.deps["policy"], b.deps["ledger"] = "up", "up"
	if d, ok := sjson.Obj(initial["dependencies"]); ok {
		for k, v := range d {
			if s, ok := v.(string); ok {
				b.deps[k] = s
			}
		}
	}
	return b
}

func (b *Broker) refuse(code string) map[string]any {
	b.refusals++
	return map[string]any{"result": "refused", "code": code}
}

func mac(key, msg []byte) string {
	h := hmac.New(sha256.New, key)
	h.Write(msg)
	return hex.EncodeToString(h.Sum(nil))
}

// sealValid checks the simulated ingress seal (D-010).
func (b *Broker) sealValid(request any, sealV any) bool {
	seal, ok := sjson.Obj(sealV)
	if !ok {
		return false
	}
	adapter, okA := sjson.Str(seal, "adapter_id")
	given, okM := sjson.Str(seal, "mac")
	key, registered := b.ingressKeys[adapter]
	if !okA || !okM || !registered {
		return false
	}
	body, err := sjson.Canonical(request)
	if err != nil {
		return false
	}
	if !hmac.Equal([]byte(mac(key, body)), []byte(given)) {
		return false
	}
	req, _ := sjson.Obj(request)
	principal, _ := sjson.Obj(req["principal"])
	return sjson.StrOr(principal, "verified_by") == adapter
}

func (b *Broker) activePolicy() *kernel.Policy {
	if b.deps["policy"] != "up" {
		return nil
	}
	return b.policy
}

func (b *Broker) decide(request any) *kernel.Decision {
	return kernel.Decide(request, b.activePolicy(), b.stateRevision, b.now)
}

func (b *Broker) bundleSha() string {
	if b.policy == nil || b.policy.Bundle == nil {
		return kernel.NoPolicySha
	}
	return b.policy.Bundle.Sha
}

// grantBinding is every field a grant binds (BROKER.md execute step 5.3).
func (b *Broker) grantBinding(request any) map[string]any {
	req, _ := sjson.Obj(request)
	action, _ := sjson.Obj(req["action"])
	principal, _ := sjson.Obj(req["principal"])
	resource, _ := sjson.Obj(action["resource"])
	caps := kernel.CapabilitySet(action)
	capsAny := make([]any, len(caps))
	for i, c := range caps {
		capsAny[i] = c
	}
	return map[string]any{
		"approval_request_id":    kernel.ApprovalRequestID(req, b.bundleSha()),
		"tenant_id":              req["tenant_id"],
		"principal_id":           principal["id"],
		"action_kind":            action["kind"],
		"canonical_resource_id":  resource["canonical_resource_id"],
		"action_digest":          action["action_digest"],
		"requested_capabilities": capsAny,
		"policy_bundle_sha":      b.bundleSha(),
	}
}

func permitBody(p *permit) []byte {
	return sjson.MustCanonical(map[string]any{
		"permit_id":           p.id,
		"request_fingerprint": p.fingerprint,
		"policy_bundle_sha":   p.bundleSha,
		"expires_at":          p.expires.UTC().Format(timeLayout),
	})
}

func requestFingerprint(request any) string {
	d, err := sjson.Digest(request)
	if err != nil {
		return "invalid"
	}
	return d
}

// adapterApply is the simulated effect adapter: it verifies the permit in a
// fixed order, records it as consumed, then applies (or fails) the effect.
func (b *Broker) adapterApply(p *permit, request any, fail bool) string {
	if requestFingerprint(request) != p.fingerprint {
		return "PERMIT_REQUEST_MISMATCH"
	}
	if !hmac.Equal([]byte(mac(b.permitKey, permitBody(p))), []byte(p.mac)) {
		return "PERMIT_INVALID"
	}
	if !b.now.Before(p.expires) {
		return "PERMIT_EXPIRED"
	}
	if b.consumed[p.id] {
		return "PERMIT_REPLAYED"
	}
	b.consumed[p.id] = true
	if !fail {
		b.effectsApplied++
	}
	return ""
}

func (b *Broker) execute(op map[string]any, step int) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	request := op["request"]
	fault := sjson.StrOr(op, "fault")
	// 1. sealed ingress
	if !b.sealValid(request, op["seal"]) {
		return b.refuse("INGRESS_UNAUTHENTICATED")
	}
	// 2. ledger availability, before any decision side effect
	if b.deps["ledger"] != "up" {
		return b.refuse("LEDGER_UNAVAILABLE")
	}
	// 3. use-time kernel decision
	d := b.decide(request)
	if d.Status != "allow" && d.Status != "require_approval" {
		return b.refuse(d.ReasonCode)
	}
	grantRef, hasGrant := sjson.Str(op, "grant_ref")
	// 4. approval required
	if d.Status == "require_approval" && !hasGrant {
		return b.refuse("APPROVAL_REQUIRED")
	}
	effective := ""
	if d.Status == "require_approval" {
		effective = grantRef
	}
	req, _ := sjson.Obj(request)
	rkey := sjson.StrOr(req, "tenant_id") + "\x00" + sjson.StrOr(req, "idempotency_key")
	fp := requestFingerprint(request)
	// 5. transaction
	r, exists := b.receipts[rkey]
	if exists {
		if r.fingerprint != fp || r.grantLabel != effective {
			return b.refuse("IDEMPOTENCY_CONFLICT")
		}
		switch r.status {
		case "observed_success":
			return map[string]any{"result": "effect_completed", "replayed": true}
		case "observed_failure":
			return map[string]any{"result": "effect_failed", "replayed": true}
		case "dispatched", "unknown_reconcile":
			return b.refuse("RECONCILIATION_REQUIRED")
		}
		// reserved: resume at step 6 without consuming the grant again
	} else {
		if fault == "crash_before_reserve" {
			return map[string]any{"result": "crashed"}
		}
		if d.Status == "require_approval" {
			g, ok := b.grants[grantRef]
			if !ok {
				return b.refuse("GRANT_UNKNOWN")
			}
			bind := b.grantBinding(request)
			if g.verdict != "approved" || string(sjson.MustCanonical(g.binding)) != string(sjson.MustCanonical(bind)) {
				return b.refuse("GRANT_MISMATCH")
			}
			if !b.now.Before(g.expires) {
				return b.refuse("GRANT_EXPIRED")
			}
			if g.uses >= g.maxUses {
				return b.refuse("GRANT_EXHAUSTED")
			}
			g.uses++
		}
		r = &receipt{fingerprint: fp, grantLabel: effective, status: "reserved"}
		b.receipts[rkey] = r
	}
	// 6. crash after reservation: the reservation and grant use persist
	if fault == "crash_after_reserve" {
		return map[string]any{"result": "crashed"}
	}
	// 7. issue a permit, mark dispatched, call the adapter
	b.permitSeq++
	p := &permit{id: fmt.Sprintf("permit-%d", b.permitSeq), fingerprint: fp, bundleSha: b.bundleSha(), expires: b.now.Add(permitTTL)}
	if s, ok := req["deadline_at"].(string); ok {
		if dl, ok := kernel.ParseTime(s); ok && dl.Before(p.expires) {
			p.expires = dl
		}
	}
	p.mac = mac(b.permitKey, permitBody(p))
	if step >= 0 {
		b.permits[step] = p
	}
	r.status = "dispatched"
	if code := b.adapterApply(p, request, fault == "effect_fails"); code != "" {
		// Unreachable for a freshly issued permit; fail closed and leave the
		// receipt dispatched so it needs reconciliation.
		return b.refuse(code)
	}
	// 8. injected faults
	if fault == "crash_after_dispatch" {
		return map[string]any{"result": "crashed"}
	}
	if fault == "effect_fails" {
		r.status = "observed_failure"
		return map[string]any{"result": "effect_failed"}
	}
	// 9. success
	r.status = "observed_success"
	return map[string]any{"result": "effect_completed"}
}

func (b *Broker) approve(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	creds, known := b.channels[sjson.StrOr(op, "channel")]
	human := sjson.StrOr(op, "human")
	cred, okC := sjson.Str(op, "credential")
	stored, okH := creds[human]
	if !known || !okC || !okH || !hmac.Equal([]byte(stored), []byte(cred)) {
		return b.refuse("HUMAN_UNAUTHENTICATED")
	}
	request := op["for_request"]
	if b.decide(request).Status != "require_approval" {
		return b.refuse("NOT_APPROVABLE")
	}
	req, _ := sjson.Obj(request)
	principal, _ := sjson.Obj(req["principal"])
	if agent, ok := req["agent_id"].(string); human == sjson.StrOr(principal, "id") || (ok && human == agent) {
		return b.refuse("SELF_APPROVAL_FORBIDDEN")
	}
	ttl := int64(defaultGrantTTL)
	if t, ok := sjson.Int(op, "ttl_seconds"); ok {
		ttl = t
	}
	// A label names one grant for the whole scenario: re-approving it would
	// reset its uses, so it is refused (protocol clarifications, M1-05).
	ref := sjson.StrOr(op, "grant_ref")
	if _, inUse := b.grants[ref]; inUse {
		return b.refuse("GRANT_REF_IN_USE")
	}
	b.grants[ref] = &grant{
		verdict: sjson.StrOr(op, "verdict"),
		binding: b.grantBinding(request),
		expires: b.now.Add(time.Duration(ttl) * time.Second),
		maxUses: 1,
	}
	return map[string]any{"result": "grant_issued"}
}

func (b *Broker) presentPermit(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	of, okOf := sjson.Int(op, "of_step")
	orig, ok := b.permits[int(of)]
	if !okOf || !ok {
		return b.refuse("PERMIT_MISSING")
	}
	p := *orig
	switch sjson.StrOr(op, "tamper") {
	case "mac":
		flipped := []byte(p.mac)
		if flipped[0] == '0' {
			flipped[0] = '1'
		} else {
			flipped[0] = '0'
		}
		p.mac = string(flipped)
	case "expiry":
		p.expires = p.expires.Add(time.Hour) // extended without re-signing
	}
	if code := b.adapterApply(&p, op["request"], false); code != "" {
		return b.refuse(code)
	}
	return map[string]any{"result": "effect_applied"}
}

func (b *Broker) simple(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch sjson.StrOr(op, "op") {
	case "apply_without_permit":
		return b.refuse("PERMIT_MISSING")
	case "activate_bundle":
		b.bundleDoc = op["bundle"]
		b.policy = kernel.LoadPolicy(b.bundleDoc)
		return map[string]any{"result": "bundle_activated"}
	case "restart_broker":
		b.restart()
		return map[string]any{"result": "restarted"}
	case "advance_time":
		if t, ok := kernel.ParseTime(sjson.StrOr(op, "now")); ok {
			b.now = t
		}
		return map[string]any{"result": "time_advanced"}
	case "set_dependency":
		b.deps[sjson.StrOr(op, "name")] = sjson.StrOr(op, "status")
		return map[string]any{"result": "dependency_set"}
	case "reconcile":
		n := int64(0)
		for _, r := range b.receipts {
			if r.status == "dispatched" {
				r.status = "unknown_reconcile"
				n++
			}
		}
		return map[string]any{"result": "reconciled", "count": n}
	}
	return b.refuse("OPERATION_UNKNOWN")
}

// restart simulates a broker process restart (D-020). Caller holds b.mu.
// Volatile state is dropped: the loaded policy cache is discarded and
// reloaded from the active bundle document. The ledger persists: grants and
// their uses, receipts and their status, consumed permit ids, the permit
// sequence (so a new permit id never reuses a consumed one) and the audit
// refusal count. Permits already handed to the client (b.permits) remain
// presentable; trusted configuration (keys, channels, dependencies, clock)
// is external to the process and unchanged.
func (b *Broker) restart() {
	b.policy = kernel.LoadPolicy(b.bundleDoc)
}

func (b *Broker) apply(op map[string]any, step int) map[string]any {
	switch sjson.StrOr(op, "op") {
	case "execute":
		return b.execute(op, step)
	case "approve":
		return b.approve(op)
	case "present_permit":
		return b.presentPermit(op)
	case "concurrent":
		return b.concurrent(op)
	}
	return b.simple(op)
}

// concurrent submits operations on real goroutines; each is one atomic
// transaction, so the outcome equals some serial order.
func (b *Broker) concurrent(op map[string]any) map[string]any {
	ops, _ := sjson.Arr(op["operations"])
	results := make([]map[string]any, len(ops))
	var wg sync.WaitGroup
	var panics goPanic
	for i, o := range ops {
		m, _ := sjson.Obj(o)
		wg.Add(1)
		go func(i int, m map[string]any) {
			defer wg.Done()
			defer panics.capture()
			results[i] = b.apply(m, -1)
		}(i, m)
	}
	wg.Wait()
	panics.rethrow()
	outcomes := map[string]any{}
	for _, r := range results {
		label, _ := r["result"].(string)
		if r["replayed"] == true {
			label = "replayed"
		} else if label == "refused" {
			label, _ = r["code"].(string)
		}
		n, _ := outcomes[label].(int64)
		outcomes[label] = n + 1
	}
	return map[string]any{"result": "concurrent", "outcomes": outcomes}
}

func (b *Broker) final() map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	uses := map[string]any{}
	labels := make([]string, 0, len(b.grants))
	for l := range b.grants {
		labels = append(labels, l)
	}
	sort.Strings(labels)
	for _, l := range labels {
		uses[l] = int64(b.grants[l].uses)
	}
	receipts := map[string]any{}
	for _, r := range b.receipts {
		n, _ := receipts[r.status].(int64)
		receipts[r.status] = n + 1
	}
	return map[string]any{
		"effects_applied":  int64(b.effectsApplied),
		"grant_uses":       uses,
		"receipts":         receipts,
		"consumed_permits": int64(len(b.consumed)),
		"refusals":         int64(b.refusals),
	}
}

// Run executes one broker scenario.
func Run(input any) map[string]any {
	in, ok := sjson.Obj(input)
	initial, okI := sjson.Obj(in["initial"])
	steps, okS := sjson.Arr(in["steps"])
	if !ok || !okI || !okS || !wellFormed(initial, steps) {
		return map[string]any{"input_error": "INPUT_MALFORMED"}
	}
	b := newBroker(initial)
	results := make([]any, 0, len(steps))
	for i, s := range steps {
		op, _ := sjson.Obj(s)
		results = append(results, b.apply(op, i))
	}
	return map[string]any{"steps": results, "final": b.final()}
}
