// Package broker runs `broker-scenario` inputs (m1/fixtures/broker/BROKER.md,
// m1-draft-3): sealed ingress, use-time kernel decisions, approval grants,
// permits, idempotent receipts, crashes and reconciliation.
//
// SIMULATED, not verified:
//   - Ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in
//     scenario state (no key management, rotation or registration).
//   - The default ledger (grants, receipts, permits) is an in-memory model
//     guarded by one mutex; every operation is one serializable transaction.
//     The SQLite ledger (LEDGER-SQLITE.md, D-023) is file-backed; with either,
//     "fault" injects a crash at a named point and nothing is killed.
//   - An "authenticated human" is a credential match on a fixture channel.
//   - Effect "dispatch" records an effect row or counter; no real side effect
//     occurs.
package broker

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
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

type permit struct {
	id          string
	fingerprint string
	bundleSha   string
	expires     time.Time
	mac         string
}

// Broker is the simulated broker for one scenario: trusted configuration and
// client-held permits in process, durable state in its ledger.
type Broker struct {
	mu sync.Mutex // configuration and client-held permits

	now           time.Time
	stateRevision int64
	bundleDoc     any            // active bundle document (persisted configuration)
	policy        *kernel.Policy // loaded active bundle (volatile cache, reloaded on restart)
	ingressKeys   map[string][]byte
	permitKey     []byte
	channels      map[string]map[string]string
	deps          map[string]string
	permits       map[int]*permit // client-held, by top-level step index

	led  ledger
	keys keyLocks

	profile Profile // concurrency profile of every concurrent group (D-027)
	// trace, when set (tests only), observes a concurrent group: "setup"
	// (worker i begins its connection setup), "ready" (worker i's setup
	// finished), "release" (i is -1) and "begin" (worker i begins a
	// transaction). It may be called from several goroutines.
	trace func(event string, worker int)
}

// Profile is the concurrency profile (LEDGER-SQLITE.md "Concurrency
// profiles", D-027): when a concurrent worker's connection is set up.
type Profile int

const (
	// Primary sets up every worker connection before the start barrier is
	// released.
	Primary Profile = iota
	// Cold sets up each worker connection after release, inside its worker.
	Cold
)

// ParseProfile is the value of `--concurrency-profile`.
func ParseProfile(s string) (Profile, bool) {
	switch s {
	case "primary":
		return Primary, true
	case "cold":
		return Cold, true
	}
	return Primary, false
}

func (b *Broker) traced(event string, worker int) {
	if b.trace != nil {
		b.trace(event, worker)
	}
}

// view is the configuration one operation runs against.
type view struct {
	now      time.Time
	rev      int64
	policy   *kernel.Policy
	policyUp bool
	ledgerUp bool
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
		permits: map[int]*permit{}, led: newMemLedger(),
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
	if d, ok := sjson.Obj(initial["dependencies"]); ok {
		for k, v := range d {
			if s, ok := v.(string); ok {
				b.deps[k] = s
			}
		}
	}
	return b
}

func (b *Broker) view() view {
	b.mu.Lock()
	defer b.mu.Unlock()
	return view{now: b.now, rev: b.stateRevision, policy: b.policy, policyUp: b.deps["policy"] == "up", ledgerUp: b.deps["ledger"] == "up"}
}

func refused(code string) map[string]any {
	return map[string]any{"result": "refused", "code": code}
}

// refuse audits a refusal in the ledger and returns it. A failed audit
// insert (rolled back, so not counted) does not change the refusal
// (LEDGER-SQLITE.md "Storage failures" rule 2).
func (b *Broker) refuse(led ledger, t *opTiming, code string) map[string]any {
	if err := led.audit(t, code); err != nil {
		diagnose(err)
	}
	return refused(code)
}

// ledgerFailed answers a ledger error (a busy timeout or a defect) with
// LEDGER_UNAVAILABLE, never a success.
func (b *Broker) ledgerFailed(led ledger, t *opTiming, err error) map[string]any {
	diagnose(err)
	return b.refuse(led, t, "LEDGER_UNAVAILABLE")
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

func (v view) decide(request any) *kernel.Decision {
	var p *kernel.Policy
	if v.policyUp {
		p = v.policy
	}
	return kernel.Decide(request, p, v.rev, v.now)
}

func (v view) bundleSha() string {
	if v.policy == nil || v.policy.Bundle == nil {
		return kernel.NoPolicySha
	}
	return v.policy.Bundle.Sha
}

// grantBinding is the D-006 canonical JSON of every field a grant binds,
// including its verdict (BROKER.md execute step 5.3).
func (v view) grantBinding(request any, verdict string) string {
	req, _ := sjson.Obj(request)
	action, _ := sjson.Obj(req["action"])
	principal, _ := sjson.Obj(req["principal"])
	resource, _ := sjson.Obj(action["resource"])
	caps := kernel.CapabilitySet(action)
	capsAny := make([]any, len(caps))
	for i, c := range caps {
		capsAny[i] = c
	}
	return string(sjson.MustCanonical(map[string]any{
		"verdict":                verdict,
		"approval_request_id":    kernel.ApprovalRequestID(req, v.bundleSha()),
		"tenant_id":              req["tenant_id"],
		"principal_id":           principal["id"],
		"action_kind":            action["kind"],
		"canonical_resource_id":  resource["canonical_resource_id"],
		"action_digest":          action["action_digest"],
		"requested_capabilities": capsAny,
		"policy_bundle_sha":      v.bundleSha(),
	}))
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

// adapterApply is the simulated effect adapter (T-adapter): inside its
// transaction it verifies the permit in a fixed order (fingerprint, MAC,
// expiry, replay), records it as consumed, then applies (or fails) the
// effect.
func (b *Broker) adapterApply(led ledger, t *opTiming, v view, p *permit, request any, fail bool) (string, error) {
	return led.adapter(t, func() string {
		if requestFingerprint(request) != p.fingerprint {
			return "PERMIT_REQUEST_MISMATCH"
		}
		if !hmac.Equal([]byte(mac(b.permitKey, permitBody(p))), []byte(p.mac)) {
			return "PERMIT_INVALID"
		}
		if !v.now.Before(p.expires) {
			return "PERMIT_EXPIRED"
		}
		return ""
	}, p.id, !fail)
}

func (b *Broker) execute(led ledger, t *opTiming, op map[string]any, step int) map[string]any {
	v := b.view()
	request := op["request"]
	fault := sjson.StrOr(op, "fault")
	// 1. sealed ingress
	validating := time.Now()
	if !b.sealValid(request, op["seal"]) {
		t.setValidate(validating)
		return b.refuse(led, t, "INGRESS_UNAUTHENTICATED")
	}
	// 2. ledger availability, before any decision side effect
	if !v.ledgerUp {
		t.setValidate(validating)
		return b.refuse(led, t, "LEDGER_UNAVAILABLE")
	}
	t.setValidate(validating)
	// 3. use-time kernel decision
	deciding := time.Now()
	d := v.decide(request)
	t.setDecide(deciding)
	if d.Status != "allow" && d.Status != "require_approval" {
		return b.refuse(led, t, d.ReasonCode)
	}
	grantRef, hasGrant := sjson.Str(op, "grant_ref")
	// 4. approval required
	if d.Status == "require_approval" && !hasGrant {
		return b.refuse(led, t, "APPROVAL_REQUIRED")
	}
	effective := ""
	if d.Status == "require_approval" {
		effective = grantRef
	}
	req, _ := sjson.Obj(request)
	tenant, key := sjson.StrOr(req, "tenant_id"), sjson.StrOr(req, "idempotency_key")
	fp := requestFingerprint(request)
	locking := time.Now()
	unlock := b.keys.lock(tenant + "\x00" + key)
	if _, isSQL := led.(*sqlLedger); isSQL {
		t.setKeyLock(locking)
	}
	defer unlock()
	// 5. T-reserve
	outcome, err := led.reserve(t, reservation{
		tenant: tenant, key: key, fingerprint: fp, grantLabel: effective,
		approval:    d.Status == "require_approval",
		crashBefore: fault == "crash_before_reserve",
		check: func(g *grant) string {
			switch {
			case g == nil:
				return "GRANT_UNKNOWN"
			case g.binding != v.grantBinding(request, "approved"):
				return "GRANT_MISMATCH"
			case !v.now.Before(g.expires):
				return "GRANT_EXPIRED"
			case g.uses >= g.maxUses:
				return "GRANT_EXHAUSTED"
			}
			return ""
		},
	})
	if err != nil {
		return b.ledgerFailed(led, t, err)
	}
	switch outcome {
	case "":
	case outcomeCrashed:
		return map[string]any{"result": "crashed"}
	case "observed_success":
		return map[string]any{"result": "effect_completed", "replayed": true}
	case "observed_failure":
		return map[string]any{"result": "effect_failed", "replayed": true}
	default:
		return b.refuse(led, t, outcome)
	}
	// 6. crash after reservation: the reservation and grant use persist
	if fault == "crash_after_reserve" {
		return map[string]any{"result": "crashed"}
	}
	// 7. T-dispatch: issue a permit and mark the receipt dispatched
	seq, err := led.dispatch(t, tenant, key)
	if err != nil {
		return b.ledgerFailed(led, t, err)
	}
	p := &permit{id: fmt.Sprintf("permit-%d", seq), fingerprint: fp, bundleSha: v.bundleSha(), expires: v.now.Add(permitTTL)}
	if s, ok := req["deadline_at"].(string); ok {
		if dl, ok := kernel.ParseTime(s); ok && dl.Before(p.expires) {
			p.expires = dl
		}
	}
	p.mac = mac(b.permitKey, permitBody(p))
	if step >= 0 {
		b.mu.Lock()
		b.permits[step] = p
		b.mu.Unlock()
	}
	// T-adapter
	code, err := b.adapterApply(led, t, v, p, request, fault == "effect_fails")
	if err != nil {
		return b.ledgerFailed(led, t, err)
	}
	if code != "" {
		// Unreachable for a freshly issued permit; fail closed and leave the
		// receipt dispatched so it needs reconciliation.
		return b.refuse(led, t, code)
	}
	// 8. injected faults
	if fault == "crash_after_dispatch" {
		return map[string]any{"result": "crashed"}
	}
	// T-observe
	status, result := "observed_success", "effect_completed"
	if fault == "effect_fails" {
		status, result = "observed_failure", "effect_failed"
	}
	if err := led.observe(t, tenant, key, status); err != nil {
		return b.ledgerFailed(led, t, err)
	}
	return map[string]any{"result": result}
}

func (b *Broker) approve(led ledger, t *opTiming, op map[string]any) map[string]any {
	v := b.view()
	validating := time.Now()
	creds, known := b.channels[sjson.StrOr(op, "channel")]
	human := sjson.StrOr(op, "human")
	cred, okC := sjson.Str(op, "credential")
	stored, okH := creds[human]
	authenticated := known && okC && okH && hmac.Equal([]byte(stored), []byte(cred))
	t.setValidate(validating)
	if !authenticated {
		return b.refuse(led, t, "HUMAN_UNAUTHENTICATED")
	}
	request := op["for_request"]
	deciding := time.Now()
	approvable := v.decide(request).Status == "require_approval"
	t.setDecide(deciding)
	if !approvable {
		return b.refuse(led, t, "NOT_APPROVABLE")
	}
	req, _ := sjson.Obj(request)
	principal, _ := sjson.Obj(req["principal"])
	if agent, ok := req["agent_id"].(string); human == sjson.StrOr(principal, "id") || (ok && human == agent) {
		return b.refuse(led, t, "SELF_APPROVAL_FORBIDDEN")
	}
	ttl := int64(defaultGrantTTL)
	if t, ok := sjson.Int(op, "ttl_seconds"); ok {
		ttl = t
	}
	// A label names one grant for the whole scenario: re-approving it would
	// reset its uses, so it is refused (protocol clarifications, M1-05).
	inUse, err := led.addGrant(t, sjson.StrOr(op, "grant_ref"), grant{
		binding: v.grantBinding(request, sjson.StrOr(op, "verdict")),
		expires: v.now.Add(time.Duration(ttl) * time.Second),
		maxUses: 1,
	})
	if err != nil {
		return b.ledgerFailed(led, t, err)
	}
	if inUse {
		return b.refuse(led, t, "GRANT_REF_IN_USE")
	}
	return map[string]any{"result": "grant_issued"}
}

func (b *Broker) presentPermit(led ledger, t *opTiming, op map[string]any) map[string]any {
	v := b.view()
	of, okOf := sjson.Int(op, "of_step")
	b.mu.Lock()
	orig, ok := b.permits[int(of)]
	b.mu.Unlock()
	if !okOf || !ok {
		return b.refuse(led, t, "PERMIT_MISSING")
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
	code, err := b.adapterApply(led, t, v, &p, op["request"], false)
	if err != nil {
		return b.ledgerFailed(led, t, err)
	}
	if code != "" {
		return b.refuse(led, t, code)
	}
	return map[string]any{"result": "effect_applied"}
}

func (b *Broker) simple(led ledger, t *opTiming, op map[string]any) map[string]any {
	switch sjson.StrOr(op, "op") {
	case "apply_without_permit":
		return b.refuse(led, t, "PERMIT_MISSING")
	case "activate_bundle":
		b.mu.Lock()
		b.bundleDoc = op["bundle"]
		b.policy = kernel.LoadPolicy(b.bundleDoc)
		b.mu.Unlock()
		return map[string]any{"result": "bundle_activated"}
	case "restart_broker":
		if err := b.restart(led); err != nil {
			return b.ledgerFailed(led, t, err)
		}
		return map[string]any{"result": "restarted"}
	case "advance_time":
		if t, ok := kernel.ParseTime(sjson.StrOr(op, "now")); ok {
			b.mu.Lock()
			b.now = t
			b.mu.Unlock()
		}
		return map[string]any{"result": "time_advanced"}
	case "set_dependency":
		b.mu.Lock()
		b.deps[sjson.StrOr(op, "name")] = sjson.StrOr(op, "status")
		b.mu.Unlock()
		return map[string]any{"result": "dependency_set"}
	case "reconcile":
		n, err := led.reconcile(t)
		if err != nil {
			return b.ledgerFailed(led, t, err)
		}
		return map[string]any{"result": "reconciled", "count": n}
	}
	return b.refuse(led, t, "OPERATION_UNKNOWN")
}

// restart simulates a broker process restart (D-020). Volatile state is
// dropped: the loaded policy cache is discarded and reloaded from the active
// bundle document, and the ledger closes and reopens its connection (key
// locks are released as each operation returns, so none is held here). The
// ledger persists: grants and their uses, receipts and their status, consumed
// permit ids, the permit sequence (so a new permit id never reuses a consumed
// one) and the audit refusal count. Permits already handed to the client
// (b.permits) remain presentable; trusted configuration (keys, channels,
// dependencies, clock) is external to the process and unchanged.
func (b *Broker) restart(led ledger) error {
	b.mu.Lock()
	b.policy = kernel.LoadPolicy(b.bundleDoc)
	b.mu.Unlock()
	return led.restart()
}

func (b *Broker) apply(op map[string]any, step int) map[string]any {
	return b.applyOn(b.led, nil, op, step)
}

// applyOn runs one operation, recording its parts in t (nil: not timed).
func (b *Broker) applyOn(led ledger, t *opTiming, op map[string]any, step int) map[string]any {
	name := sjson.StrOr(op, "op")
	if name == "concurrent" {
		return b.concurrent(led, t, op)
	}
	defer led.serialize()()
	switch name {
	case "execute":
		return b.execute(led, t, op, step)
	case "approve":
		return b.approve(led, t, op)
	case "present_permit":
		return b.presentPermit(led, t, op)
	}
	return b.simple(led, t, op)
}

// concurrent submits operations on real goroutines, each with its own ledger
// connection set up for the group and closed afterwards; all start together
// when close(start) releases them. The ledger's transactions and key locks
// make the outcome equal some serial order.
//
// Concurrency profiles (D-027): Primary sets up every worker connection
// (sequentially, in this goroutine) before the goroutines are created and
// released; Cold creates the goroutines, releases them, and each sets up its
// own connection. A worker whose setup failed answers LEDGER_UNAVAILABLE with
// no transaction and no audit.
//
// Timing: ready_ns is taken before close(start): in Primary the latest setup
// end, in Cold (and for the in-memory ledger) the latest instant a worker's
// goroutine was created with its operation. Each worker's start_ns is read
// after it passed the barrier, so start_ns >= ready_ns. A worker's total_ns
// runs from passing the barrier to its result (in Cold it includes the
// setup); the barrier wait is in neither.
func (b *Broker) concurrent(led ledger, t *opTiming, op map[string]any) map[string]any {
	ops, _ := sjson.Arr(op["operations"])
	results := make([]map[string]any, len(ops))
	workers := make([]ledger, len(ops))
	timings := make([]*opTiming, len(ops))
	if t != nil {
		t.concurrent = true
		for i, o := range ops {
			m, _ := sjson.Obj(o)
			timings[i] = t.worker(m)
		}
	}
	_, isSQL := led.(*sqlLedger)
	cold := b.profile == Cold && isSQL
	// setup is worker i's connection setup: on success workers[i] is set.
	setup := func(i int) time.Time {
		b.traced("setup", i)
		opening := time.Now()
		w, err := led.fork()
		done := time.Now()
		if isSQL {
			timings[i].setSetup(done.Sub(opening))
		}
		if err != nil {
			diagnose(err)
		} else {
			if s, ok := w.(*sqlLedger); ok && b.trace != nil {
				s.onBegin = func() { b.traced("begin", i) }
			}
			workers[i] = w
		}
		b.traced("ready", i)
		return done
	}
	var ready time.Time
	if !cold {
		for i := range ops {
			if done := setup(i); isSQL {
				ready = done
			}
		}
	}
	start := make(chan struct{})
	var wg sync.WaitGroup
	var panics goPanic
	for i, o := range ops {
		m, _ := sjson.Obj(o)
		wg.Add(1)
		go func(i int, m map[string]any) {
			defer wg.Done()
			defer panics.capture()
			<-start
			running := time.Now()
			timings[i].setStart(t, running)
			timings[i].setDispatch(running)
			if cold {
				setup(i)
			}
			if workers[i] == nil {
				results[i] = refused("LEDGER_UNAVAILABLE")
			} else {
				results[i] = b.applyOn(workers[i], timings[i], m, -1)
			}
			timings[i].setTotal(time.Since(running))
		}(i, m)
		if cold || !isSQL {
			ready = time.Now()
		}
	}
	if len(ops) > 0 {
		t.setReady(ready)
	}
	b.traced("release", -1)
	close(start)
	wg.Wait()
	for _, w := range workers {
		if w != nil && w != led {
			w.close()
		}
	}
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
	defer b.led.serialize()()
	f, err := b.led.final()
	if err != nil {
		diagnose(err)
		return map[string]any{"error": "LEDGER_UNAVAILABLE"}
	}
	return f
}

// Run executes one broker scenario on the in-memory ledger.
func Run(input any) map[string]any {
	return RunWith(input, nil)
}

// RunWith executes one broker scenario on the SQLite ledger store selects, or
// on the in-memory ledger when store is nil.
func RunWith(input any, store *SQLite) map[string]any {
	return RunTimed(input, store, nil)
}

// RunTimed is RunWith recording per-operation timing in tm (nil: not timed;
// the output is the same either way). tm.Done is called when the output
// value is complete, before the ledger closes.
func RunTimed(input any, store *SQLite, tm *Timing) map[string]any {
	return RunProfile(input, store, Primary, tm)
}

// RunProfile is RunTimed with concurrency profile prof (D-027).
func RunProfile(input any, store *SQLite, prof Profile, tm *Timing) map[string]any {
	in, ok := sjson.Obj(input)
	initial, okI := sjson.Obj(in["initial"])
	steps, okS := sjson.Arr(in["steps"])
	if !ok || !okI || !okS || !wellFormed(initial, steps) {
		tm.Done()
		return map[string]any{"input_error": "INPUT_MALFORMED"}
	}
	b := newBroker(initial)
	b.profile = prof
	if store != nil {
		opening := time.Now()
		led, err := store.open()
		if tm != nil {
			tm.open = since(opening)
		}
		if err != nil {
			diagnose(err)
			tm.Done()
			return map[string]any{"error": "LEDGER_UNAVAILABLE"}
		}
		b.led = led
	}
	defer b.led.close()
	results := make([]any, 0, len(steps))
	for i, s := range steps {
		op, _ := sjson.Obj(s)
		st := tm.step(op)
		started := time.Now()
		st.setDispatch(started)
		results = append(results, b.applyOn(b.led, st, op, i))
		st.setTotal(time.Since(started))
	}
	finalizing := time.Now()
	f := b.final()
	if tm != nil {
		tm.final = since(finalizing)
	}
	out := map[string]any{"steps": results, "final": f}
	tm.Done()
	return out
}
