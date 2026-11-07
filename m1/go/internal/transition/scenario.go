// Package transition runs `transition-scenario` inputs: the broker side of a
// workflow promotion (m1/fixtures/transition/SCENARIOS.md, m1-draft-4).
//
// SIMULATED: the ledger is an in-memory model guarded by one mutex; every
// operation is one serializable transaction. "fault" values inject crashes at
// named points; effect "dispatch" only changes the in-memory effect store. No
// durable storage, crash recovery or multi-process locking is exercised here.
package transition

import (
	"fmt"
	"sort"
	"sync"

	"writ.dev/m1/proto/internal/acceptance"
	"writ.dev/m1/proto/internal/sjson"
)

const transitionKind = "workflow.transition"

type promotionReceipt struct {
	fingerprint string
	result      map[string]any
}

type effectEntry struct {
	fingerprint string
	status      string // completed | unknown_reconcile
}

// Broker is the trusted broker state for one scenario (simulated ledger).
type Broker struct {
	mu sync.Mutex

	tenant, project, task, workflow string
	state                           string
	revision                        int64
	snapshot, artifactDigest        string
	policyID, policyVersion         string
	policyBundle                    string
	requirements                    any
	authBundle                      string
	protection                      any // as given; nil when absent
	effectPrereqs                   map[string]any
	lastPromotion                   map[string]any
	now                             string
	consumed                        map[string]bool
	revoked                         []string

	receipts map[string]promotionReceipt
	effects  map[string]effectEntry
	refusals int
}

func strList(v any) []string {
	ss, _ := sjson.Strings(v)
	return ss
}

func newBroker(initial map[string]any) *Broker {
	b := &Broker{
		tenant: sjson.StrOr(initial, "tenant_id"), project: sjson.StrOr(initial, "project_id"),
		task: sjson.StrOr(initial, "task_id"), workflow: sjson.StrOr(initial, "workflow_id"),
		state:    sjson.StrOr(initial, "state"),
		snapshot: sjson.StrOr(initial, "artifact_snapshot_id"), artifactDigest: sjson.StrOr(initial, "artifact_digest"),
		policyID: sjson.StrOr(initial, "acceptance_policy_id"), policyVersion: sjson.StrOr(initial, "acceptance_policy_version"),
		policyBundle: sjson.StrOr(initial, "acceptance_policy_bundle_sha"),
		requirements: initial["acceptance_requirements"],
		authBundle:   sjson.StrOr(initial, "authorization_bundle_sha"),
		protection:   initial["protection"],
		now:          sjson.StrOr(initial, "now"),
		consumed:     map[string]bool{},
		revoked:      strList(initial["revoked_evaluator_ids"]),
		receipts:     map[string]promotionReceipt{},
		effects:      map[string]effectEntry{},
	}
	b.revision, _ = sjson.Int(initial, "revision")
	b.effectPrereqs, _ = sjson.Obj(initial["effect_prerequisites"])
	b.lastPromotion, _ = sjson.Obj(initial["last_promotion"])
	for _, id := range strList(initial["consumed_record_ids"]) {
		b.consumed[id] = true
	}
	if b.revoked == nil {
		b.revoked = []string{}
	}
	return b
}

func (b *Broker) refuse(code string) map[string]any {
	b.refusals++
	return map[string]any{"result": "refused", "code": code}
}

func toAny(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

func (b *Broker) consumedSorted() []string {
	out := make([]string, 0, len(b.consumed))
	for id := range b.consumed {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// current builds the trusted `current` input of check_acceptance from broker state.
func (b *Broker) current(from, to string) map[string]any {
	return map[string]any{
		"tenant_id": b.tenant, "project_id": b.project, "task_id": b.task, "workflow_id": b.workflow,
		"transition":           map[string]any{"from_state": from, "to_state": to},
		"artifact_snapshot_id": b.snapshot, "artifact_digest": b.artifactDigest,
		"acceptance_policy_id": b.policyID, "acceptance_policy_version": b.policyVersion,
		"acceptance_policy_bundle_sha": b.policyBundle,
		"state_revision":               b.revision,
		"now":                          b.now,
		"consumed_record_ids":          toAny(b.consumedSorted()),
		"revoked_evaluator_ids":        toAny(b.revoked),
	}
}

type classification struct {
	authBundle, acceptanceBundle string
	states                       map[string]bool
	edges                        map[[2]string]bool
}

func parseProtection(v any) (*classification, bool) {
	m, ok := sjson.Obj(v)
	if !ok {
		return nil, false
	}
	c := &classification{states: map[string]bool{}, edges: map[[2]string]bool{}}
	var okA, okB bool
	c.authBundle, okA = sjson.Str(m, "authorization_bundle_sha")
	c.acceptanceBundle, okB = sjson.Str(m, "acceptance_policy_bundle_sha")
	states, okS := sjson.Strings(m["protected_to_states"])
	edges, okE := sjson.Arr(m["protected_transitions"])
	if !okA || !okB || !okS || !okE {
		return nil, false
	}
	for _, s := range states {
		c.states[s] = true
	}
	for _, ev := range edges {
		e, ok := sjson.Obj(ev)
		from, okF := sjson.Str(e, "from_state")
		to, okT := sjson.Str(e, "to_state")
		if !ok || !okF || !okT {
			return nil, false
		}
		c.edges[[2]string{from, to}] = true
	}
	return c, true
}

func fingerprint(op map[string]any) string {
	d, err := sjson.Digest(sjson.Without(op, "fault"))
	if err != nil {
		return "invalid"
	}
	return d
}

func requestDigest(v any) string {
	d, err := sjson.Digest(v)
	if err != nil {
		return "invalid"
	}
	return d
}

var acceptanceAttrs = []string{"acceptance_record_id", "artifact_snapshot_id", "artifact_digest"}

func (b *Broker) promote(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	key := sjson.StrOr(op, "idempotency_key")
	fp := fingerprint(op)
	// 1. idempotency
	if r, ok := b.receipts[key]; ok {
		if r.fingerprint != fp {
			return b.refuse("IDEMPOTENCY_CONFLICT")
		}
		out := sjson.Clone(r.result).(map[string]any)
		out["replayed"] = true
		return out
	}
	// 2..4 permit
	permit, ok := sjson.Obj(op["permit"])
	if !ok {
		return b.refuse("PERMIT_MISSING")
	}
	request := op["request"]
	if sjson.StrOr(permit, "request_fingerprint") != requestDigest(request) {
		return b.refuse("PERMIT_REQUEST_MISMATCH")
	}
	if sjson.StrOr(permit, "authorization_bundle_sha") != b.authBundle {
		return b.refuse("PERMIT_POLICY_STALE")
	}
	// 5. policy view consistency
	prot, ok := parseProtection(b.protection)
	if !ok || prot.authBundle != b.authBundle {
		return b.refuse("PROTECTION_UNAVAILABLE")
	}
	if prot.acceptanceBundle != b.policyBundle {
		return b.refuse("POLICY_DEPENDENCY_MISMATCH")
	}
	// 6. request shape
	req, _ := sjson.Obj(request)
	action, _ := sjson.Obj(req["action"])
	resource, _ := sjson.Obj(action["resource"])
	wantResource := fmt.Sprintf("workflow:%s/%s/%s", b.tenant, b.project, b.workflow)
	if sjson.StrOr(action, "kind") != transitionKind || sjson.StrOr(req, "tenant_id") != b.tenant ||
		sjson.StrOr(req, "project_id") != b.project || sjson.StrOr(resource, "canonical_resource_id") != wantResource {
		return b.refuse("REQUEST_SCOPE_MISMATCH")
	}
	caps, _ := sjson.Arr(action["requested_capabilities"])
	for _, c := range caps {
		if c == transitionKind {
			return b.refuse("REQUESTED_CAPABILITIES_INVALID")
		}
	}
	if len(caps) != 0 {
		return b.refuse("COMPOSITE_NOT_SUPPORTED")
	}
	// 7. protection from the version-pinned classification (union)
	attrs, _ := sjson.Obj(action["attributes"])
	from := sjson.StrOr(attrs, "from_state")
	to := sjson.StrOr(attrs, "to_state")
	protected := prot.states[to] || prot.edges[[2]string{from, to}]
	present := 0
	for _, k := range acceptanceAttrs {
		if sjson.Has(attrs, k) {
			present++
		}
	}
	record := op["record"]
	if protected {
		if present != len(acceptanceAttrs) || record == nil {
			return b.refuse("ACCEPTANCE_REQUIRED")
		}
		rec, _ := sjson.Obj(record)
		pd, okP := sjson.Str(permit, "record_digest")
		rd, okR := sjson.Str(rec, "record_digest")
		if !okP || !okR || pd != rd {
			return b.refuse("PERMIT_BINDING_MISMATCH")
		}
		for _, k := range acceptanceAttrs {
			a, okA := sjson.Str(attrs, k)
			r, okR := sjson.Str(rec, k)
			if !okA || !okR || a != r {
				return b.refuse("PERMIT_BINDING_MISMATCH")
			}
		}
	} else if present != 0 || record != nil || permit["record_digest"] != nil {
		return b.refuse("ACCEPTANCE_BINDING_UNEXPECTED")
	}
	// 8. compare-and-set
	if exp, ok := sjson.Int(req, "expected_state_revision"); !ok || exp != b.revision || from != b.state {
		return b.refuse("REVISION_CONFLICT")
	}
	// 9. acceptance record (protected only)
	if protected {
		if code := acceptance.Check(record, b.current(from, to), b.requirements); code != "" {
			return b.refuse(code)
		}
	}
	// 10. injected crash: the transaction aborts and nothing persists
	if sjson.StrOr(op, "fault") == "crash_before_commit" {
		return map[string]any{"result": "crashed"}
	}
	// 11. commit
	b.state = to
	b.revision++
	result := map[string]any{"result": "committed", "revision": b.revision}
	b.receipts[key] = promotionReceipt{fingerprint: fp, result: result}
	if protected {
		rec, _ := sjson.Obj(record)
		recordID := sjson.StrOr(rec, "acceptance_record_id")
		b.consumed[recordID] = true
		b.lastPromotion = map[string]any{
			"receipt_id":                   fmt.Sprintf("promotion:%s:%d", b.workflow, b.revision),
			"record_id":                    recordID,
			"artifact_snapshot_id":         b.snapshot,
			"artifact_digest":              b.artifactDigest,
			"acceptance_policy_bundle_sha": b.policyBundle,
			"authorization_bundle_sha":     b.authBundle,
			"to_state":                     to,
		}
	}
	return sjson.Clone(result).(map[string]any)
}

// effect is a simulated governed effect (for example a publish after promotion).
func (b *Broker) effect(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	key := sjson.StrOr(op, "idempotency_key")
	fp := fingerprint(op)
	// 1. idempotency
	if e, ok := b.effects[key]; ok {
		if e.fingerprint != fp {
			return b.refuse("IDEMPOTENCY_CONFLICT")
		}
		if e.status == "completed" {
			return map[string]any{"result": "effect_completed", "replayed": true}
		}
		return b.refuse("RECONCILIATION_REQUIRED")
	}
	// 2. permit (pre-reservation; the key stays unbound)
	permit, ok := sjson.Obj(op["permit"])
	if !ok {
		return b.refuse("PERMIT_MISSING")
	}
	request := op["request"]
	if sjson.StrOr(permit, "request_fingerprint") != requestDigest(request) {
		return b.refuse("PERMIT_REQUEST_MISMATCH")
	}
	if sjson.StrOr(permit, "authorization_bundle_sha") != b.authBundle {
		return b.refuse("PERMIT_POLICY_STALE")
	}
	// 3. trusted prerequisites (D-019 N-3)
	req, _ := sjson.Obj(request)
	action, _ := sjson.Obj(req["action"])
	if prereq, listed := sjson.Obj(b.effectPrereqs[sjson.StrOr(action, "kind")]); listed {
		attrs, _ := sjson.Obj(action["attributes"])
		var vals [4]string
		for i, k := range []string{"workflow_id", "artifact_snapshot_id", "artifact_digest", "promotion_receipt_id"} {
			s, ok := sjson.Str(attrs, k)
			if !ok {
				return b.refuse("PROMOTION_RECEIPT_INVALID")
			}
			vals[i] = s
		}
		wf, snap, digest, receipt := vals[0], vals[1], vals[2], vals[3]
		if wf != b.workflow {
			return b.refuse("REQUEST_SCOPE_MISMATCH")
		}
		inState := false
		for _, s := range strList(prereq["required_states"]) {
			if s == b.state {
				inState = true
			}
		}
		if !inState {
			return b.refuse("EFFECT_PREREQUISITE_UNMET")
		}
		lp := b.lastPromotion
		if lp == nil || sjson.StrOr(lp, "receipt_id") != receipt {
			return b.refuse("PROMOTION_RECEIPT_INVALID")
		}
		if snap != sjson.StrOr(lp, "artifact_snapshot_id") || digest != sjson.StrOr(lp, "artifact_digest") {
			return b.refuse("EFFECT_ARTIFACT_MISMATCH")
		}
		if sjson.StrOr(lp, "artifact_snapshot_id") != b.snapshot || sjson.StrOr(lp, "artifact_digest") != b.artifactDigest ||
			sjson.StrOr(lp, "acceptance_policy_bundle_sha") != b.policyBundle ||
			sjson.StrOr(lp, "authorization_bundle_sha") != b.authBundle {
			return b.refuse("PROMOTION_STALE")
		}
	}
	fault := sjson.StrOr(op, "fault")
	// 4. crash before reservation: nothing persists
	if fault == "crash_before_reserve" {
		return map[string]any{"result": "crashed"}
	}
	// 5. reserve and dispatch (simulated: no external system is called)
	if fault == "crash_after_dispatch" {
		b.effects[key] = effectEntry{fingerprint: fp, status: "unknown_reconcile"}
		return map[string]any{"result": "crashed"}
	}
	// 6. complete
	b.effects[key] = effectEntry{fingerprint: fp, status: "completed"}
	return map[string]any{"result": "effect_completed"}
}

func (b *Broker) reconcile(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	key := sjson.StrOr(op, "idempotency_key")
	e, ok := b.effects[key]
	if !ok || e.status != "unknown_reconcile" {
		return b.refuse("NOTHING_TO_RECONCILE")
	}
	observed := sjson.StrOr(op, "observed")
	switch observed {
	case "applied":
		e.status = "completed"
		b.effects[key] = e
	case "not_applied":
		delete(b.effects, key)
	default:
		return b.refuse("RECONCILE_OBSERVATION_INVALID")
	}
	return map[string]any{"result": "reconciled", "outcome": observed}
}

func (b *Broker) simple(op map[string]any) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch sjson.StrOr(op, "op") {
	case "update_artifact":
		b.snapshot = sjson.StrOr(op, "artifact_snapshot_id")
		b.artifactDigest = sjson.StrOr(op, "artifact_digest")
		b.revision++
		return map[string]any{"result": "artifact_updated", "revision": b.revision}
	case "activate_policy":
		if id, ok := sjson.Str(op, "acceptance_policy_id"); ok {
			b.policyID = id
		}
		b.policyVersion = sjson.StrOr(op, "acceptance_policy_version")
		b.policyBundle = sjson.StrOr(op, "acceptance_policy_bundle_sha")
		b.requirements = op["acceptance_requirements"]
		return map[string]any{"result": "policy_activated"}
	case "activate_authorization_bundle":
		b.authBundle = sjson.StrOr(op, "authorization_bundle_sha")
		b.protection = op["protection"]
		return map[string]any{"result": "authorization_bundle_activated"}
	case "revoke_evaluator":
		b.revoked = append(b.revoked, sjson.StrOr(op, "evaluator_id"))
		return map[string]any{"result": "evaluator_revoked"}
	case "advance_time":
		b.now = sjson.StrOr(op, "now")
		return map[string]any{"result": "time_advanced"}
	}
	return b.refuse("OPERATION_UNKNOWN")
}

func (b *Broker) apply(op map[string]any) map[string]any {
	switch sjson.StrOr(op, "op") {
	case "promote":
		return b.promote(op)
	case "effect":
		return b.effect(op)
	case "reconcile":
		return b.reconcile(op)
	case "concurrent":
		return b.concurrent(op)
	case "promote_concurrent":
		return b.promoteConcurrent(op)
	}
	return b.simple(op)
}

// concurrent submits operations on real goroutines; each operation is one
// atomic transaction, so the outcome equals some serialization.
func (b *Broker) concurrent(op map[string]any) map[string]any {
	ops, _ := sjson.Arr(op["operations"])
	results := make([]any, len(ops))
	var wg sync.WaitGroup
	var panics goPanic
	for i, o := range ops {
		m, _ := sjson.Obj(o)
		wg.Add(1)
		go func(i int, m map[string]any) {
			defer wg.Done()
			defer panics.capture()
			results[i] = b.apply(m)
		}(i, m)
	}
	wg.Wait()
	panics.rethrow()
	return map[string]any{"result": "concurrent", "results": results}
}

func (b *Broker) promoteConcurrent(op map[string]any) map[string]any {
	count, _ := sjson.Int(op, "count")
	tmpl, _ := sjson.Obj(op["promote"])
	key := sjson.StrOr(tmpl, "idempotency_key")
	results := make([]map[string]any, count)
	var wg sync.WaitGroup
	var panics goPanic
	for i := int64(0); i < count; i++ {
		p := sjson.Without(tmpl)
		p["idempotency_key"] = fmt.Sprintf("%s-%d", key, i+1)
		wg.Add(1)
		go func(i int64, p map[string]any) {
			defer wg.Done()
			defer panics.capture()
			results[i] = b.promote(p)
		}(i, p)
	}
	wg.Wait()
	panics.rethrow()
	committed := int64(0)
	refused := map[string]any{}
	for _, r := range results {
		switch r["result"] {
		case "committed":
			committed++
		case "refused":
			code := r["code"].(string)
			n, _ := refused[code].(int64)
			refused[code] = n + 1
		}
	}
	return map[string]any{"result": "concurrent", "committed": committed, "refused": refused}
}

func (b *Broker) final() map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	effects := map[string]any{}
	for k, e := range b.effects {
		effects[k] = e.status
	}
	return map[string]any{
		"state":               b.state,
		"revision":            b.revision,
		"consumed_record_ids": toAny(b.consumedSorted()),
		"receipts":            int64(len(b.receipts)),
		"refusals":            int64(b.refusals),
		"effects":             effects,
	}
}

// Run executes one scenario input.
func Run(input any) map[string]any {
	in, ok := sjson.Obj(input)
	initial, okI := sjson.Obj(in["initial"])
	steps, okS := sjson.Arr(in["steps"])
	if !ok || !okI || !okS || !wellFormed(initial, steps) {
		return map[string]any{"input_error": "INPUT_MALFORMED"}
	}
	b := newBroker(initial)
	results := make([]any, 0, len(steps))
	for _, s := range steps {
		op, _ := sjson.Obj(s)
		results = append(results, b.apply(op))
	}
	return map[string]any{"steps": results, "final": b.final()}
}
