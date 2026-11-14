// Package kernel is the pure decision function (D-008 order, extended by the
// M1 transition rules in m1/fixtures/transition/SCENARIOS.md). It performs no
// I/O and reads no clock: time is an input. It imports no model SDK.
package kernel

import (
	"sort"
	"strings"
	"time"

	"writ.dev/m1/proto/internal/sjson"
)

const (
	NoPolicySha = "none"
	decisionTTL = 5 * time.Minute
	timeLayout  = "2006-01-02T15:04:05Z"
)

// Policy is the policy view handed to the kernel: nil means no policy is
// available; Bundle nil with a non-nil Policy means the bundle was rejected
// at load.
type Policy struct {
	Bundle *Bundle
}

// LoadPolicy loads a bundle document into a policy view (nil document: nil).
func LoadPolicy(doc any) *Policy {
	if doc == nil {
		return nil
	}
	return &Policy{Bundle: LoadBundle(doc)}
}

// Decision is the kernel output.
type Decision struct {
	RequestID       string
	Status          string
	ReasonCode      string
	MatchedRuleIDs  []string
	PolicySha       string
	StateRevision   int64
	DecidedAt       time.Time
	ExpiresAt       *time.Time
	ApprovalRequest string // "" when none
}

// JSON renders the decision as a Decision contract object.
func (d *Decision) JSON() map[string]any {
	matched := make([]any, len(d.MatchedRuleIDs))
	for i, m := range d.MatchedRuleIDs {
		matched[i] = m
	}
	var expires, approval any
	if d.ExpiresAt != nil {
		expires = d.ExpiresAt.UTC().Format(timeLayout)
	}
	if d.ApprovalRequest != "" {
		approval = d.ApprovalRequest
	}
	id := sjson.MustDigest(map[string]any{
		"request": d.RequestID, "policy": d.PolicySha, "revision": d.StateRevision,
		"status": d.Status, "reason": d.ReasonCode, "at": d.DecidedAt.UTC().Format(timeLayout),
	})
	return map[string]any{
		"contract_version":           "2.0",
		"decision_id":                "dec:" + strings.TrimPrefix(id, sjson.DigestPrefix)[:32],
		"request_id":                 d.RequestID,
		"status":                     d.Status,
		"policy_bundle_sha":          d.PolicySha,
		"reason_code":                d.ReasonCode,
		"safe_message":               nil,
		"matched_rule_ids":           matched,
		"state_revision":             d.StateRevision,
		"decided_at":                 d.DecidedAt.UTC().Format(timeLayout),
		"permit_reference":           nil,
		"approval_request_reference": approval,
		"expires_at":                 expires,
	}
}

func isCanonicalID(id string) bool {
	scheme, rest, found := strings.Cut(id, ":")
	if !found || scheme == "" || rest == "" || strings.Contains(rest, `\`) {
		return false
	}
	segs := strings.Split(rest, "/")
	if len(segs) < 3 {
		return false
	}
	for _, s := range segs {
		if s == "" || s == "." || s == ".." {
			return false
		}
	}
	return true
}

func inScope(id, tenant, project string) bool {
	if strings.Contains(tenant, "/") || strings.Contains(project, "/") {
		return false
	}
	_, rest, _ := strings.Cut(id, ":")
	return strings.HasPrefix(rest, tenant+"/"+project+"/")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// CapabilitySet is the sorted, unique requested capability set (SR-02).
func CapabilitySet(action map[string]any) []string {
	caps, _ := sjson.Strings(action["requested_capabilities"])
	set := map[string]bool{}
	for _, c := range caps {
		set[c] = true
	}
	out := make([]string, 0, len(set))
	for c := range set {
		out = append(out, c)
	}
	sort.Strings(out)
	return out
}

func toAny(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

// ApprovalRequestID binds an approval to the exact operation and policy
// version; for transitions it also binds from_state and to_state.
func ApprovalRequestID(req map[string]any, bundleSha string) string {
	action, _ := sjson.Obj(req["action"])
	principal, _ := sjson.Obj(req["principal"])
	resource, _ := sjson.Obj(action["resource"])
	binding := map[string]any{
		"tenant_id":              sjson.StrOr(req, "tenant_id"),
		"principal_id":           sjson.StrOr(principal, "id"),
		"action_kind":            sjson.StrOr(action, "kind"),
		"canonical_resource_id":  resource["canonical_resource_id"],
		"action_digest":          sjson.StrOr(action, "action_digest"),
		"requested_capabilities": toAny(CapabilitySet(action)),
		"policy_bundle_sha":      bundleSha,
	}
	if sjson.StrOr(action, "kind") == TransitionKind {
		attrs, _ := sjson.Obj(action["attributes"])
		binding["from_state"] = attrs["from_state"]
		binding["to_state"] = attrs["to_state"]
	}
	d := sjson.MustDigest(binding)
	return "apr:" + strings.TrimPrefix(d, sjson.DigestPrefix)[:32]
}

var acceptanceAttrs = []string{"acceptance_record_id", "artifact_snapshot_id", "artifact_digest"}

// transitionAttrs validates the closed transition attribute set; it returns
// from, to, whether the acceptance binding is present, and validity.
func transitionAttrs(action map[string]any) (from, to string, bound, ok bool) {
	attrs, isObj := sjson.Obj(action["attributes"])
	if !isObj {
		return "", "", false, false
	}
	for k := range attrs {
		if k != "from_state" && k != "to_state" && !contains(acceptanceAttrs, k) {
			return "", "", false, false
		}
	}
	from, okF := attrs["from_state"].(string)
	to, okT := attrs["to_state"].(string)
	if !okF || !okT || !StatePattern.MatchString(from) || !StatePattern.MatchString(to) || from == to {
		return "", "", false, false
	}
	present := 0
	for _, k := range acceptanceAttrs {
		if v, has := attrs[k]; has {
			if _, isStr := v.(string); !isStr {
				return "", "", false, false
			}
			present++
		}
	}
	if present != 0 && present != len(acceptanceAttrs) {
		return "", "", false, false
	}
	return from, to, present == len(acceptanceAttrs), true
}

func statementApplies(s *Statement, req map[string]any, kinds map[string]bool, kind, from, to string) bool {
	if s.hasTenants && !contains(s.Tenants, sjson.StrOr(req, "tenant_id")) {
		return false
	}
	if s.hasRoles {
		principal, _ := sjson.Obj(req["principal"])
		roles, _ := sjson.Strings(principal["roles"])
		hit := false
		for _, r := range roles {
			if contains(s.PrincipalRoles, r) {
				hit = true
			}
		}
		if !hit {
			return false
		}
	}
	hit := false
	for _, k := range s.ActionKinds {
		if kinds[k] {
			hit = true
		}
	}
	if !hit {
		return false
	}
	if s.hasPrefixes {
		action, _ := sjson.Obj(req["action"])
		resource, _ := sjson.Obj(action["resource"])
		id, _ := resource["canonical_resource_id"].(string)
		ok := false
		for _, p := range s.ResourcePrefixes {
			if strings.HasPrefix(id, p) {
				ok = true
			}
		}
		if !ok {
			return false
		}
	}
	// State conditions match only workflow.transition requests whose
	// attribute is listed.
	if s.FromStates != nil && (kind != TransitionKind || !contains(s.FromStates, from)) {
		return false
	}
	if s.ToStates != nil && (kind != TransitionKind || !contains(s.ToStates, to)) {
		return false
	}
	return true
}

// Decide is the pure kernel decision. request is an untrusted parsed value.
func Decide(request any, policy *Policy, stateRevision int64, now time.Time) *Decision {
	d := &Decision{RequestID: "unknown", PolicySha: NoPolicySha, StateRevision: stateRevision, DecidedAt: now, MatchedRuleIDs: []string{}}
	if policy != nil && policy.Bundle != nil {
		d.PolicySha = policy.Bundle.Sha
	}
	if req, ok := sjson.Obj(request); ok {
		if rid, ok := req["request_id"].(string); ok && rid != "" {
			d.RequestID = rid
		}
	}
	var deadline *time.Time
	result := func(status, reason string, matched []string, approval string) *Decision {
		d.Status, d.ReasonCode, d.ApprovalRequest = status, reason, approval
		if matched != nil {
			d.MatchedRuleIDs = append([]string(nil), matched...)
			sort.Strings(d.MatchedRuleIDs)
		}
		if status == "allow" || status == "require_approval" {
			exp := now.Add(decisionTTL)
			if deadline != nil && deadline.Before(exp) {
				exp = *deadline
			}
			d.ExpiresAt = &exp
		}
		return d
	}

	if !ValidRequest(request) {
		return result("invalid", "REQUEST_SCHEMA_INVALID", nil, "")
	}
	req := request.(map[string]any)
	action := req["action"].(map[string]any)
	kind := action["kind"].(string)
	caps, _ := sjson.Strings(action["requested_capabilities"])
	if contains(caps, kind) {
		return result("invalid", "REQUESTED_CAPABILITIES_INVALID", nil, "")
	}
	if s, ok := req["deadline_at"].(string); ok {
		t, _ := ParseTime(s)
		deadline = &t
		if !t.After(now) {
			return result("invalid", "DEADLINE_EXCEEDED", nil, "")
		}
	}
	if policy == nil {
		return result("unavailable", "POLICY_UNAVAILABLE", nil, "")
	}
	b := policy.Bundle
	if b == nil {
		return result("unavailable", "POLICY_BUNDLE_REJECTED", nil, "")
	}
	if pin, ok := req["policy_bundle_sha"].(string); ok && pin != b.Sha {
		return result("unavailable", "POLICY_VERSION_MISMATCH", nil, "")
	}
	principal := req["principal"].(map[string]any)
	if !b.Issuers[principal["verified_by"].(string)] {
		return result("deny", "PRINCIPAL_UNVERIFIED", nil, "")
	}
	if exp, ok := req["expected_state_revision"].(int64); ok && exp != stateRevision {
		return result("invalid", "STATE_REVISION_CONFLICT", nil, "")
	}
	kinds := map[string]bool{kind: true}
	for _, c := range caps {
		kinds[c] = true
	}
	for k := range kinds {
		if !b.Known[k] {
			return result("deny", "UNKNOWN_CAPABILITY", nil, "")
		}
	}
	resource := action["resource"].(map[string]any)
	rid, _ := resource["canonical_resource_id"].(string)
	if rid == "" || !isCanonicalID(rid) {
		return result("invalid", "RESOURCE_NOT_CANONICAL", nil, "")
	}
	tenant, project := req["tenant_id"].(string), req["project_id"].(string)
	if !inScope(rid, tenant, project) {
		return result("deny", "RESOURCE_SCOPE_MISMATCH", nil, "")
	}

	var from, to string
	if kind == TransitionKind {
		// Known but not supported by this bundle (D-020); an unknown
		// capability was already denied above.
		if !b.TransitionSupported {
			return result("deny", "UNSUPPORTED_CAPABILITY", nil, "")
		}
		scheme, rest, _ := strings.Cut(rid, ":")
		segs := strings.Split(rest, "/")
		if scheme != "workflow" || len(segs) != 3 || !StatePattern.MatchString(segs[2]) {
			return result("invalid", "WORKFLOW_RESOURCE_INVALID", nil, "")
		}
		var bound, ok bool
		from, to, bound, ok = transitionAttrs(action)
		if !ok {
			return result("invalid", "TRANSITION_MALFORMED", nil, "")
		}
		if len(caps) != 0 {
			return result("invalid", "COMPOSITE_NOT_SUPPORTED", nil, "")
		}
		protected := b.Protection.IsProtected(from, to)
		if protected && !bound {
			return result("invalid", "ACCEPTANCE_BINDING_REQUIRED", nil, "")
		}
		if !protected && bound {
			return result("invalid", "ACCEPTANCE_BINDING_UNEXPECTED", nil, "")
		}
	}

	var forbids, approvals, permits []*Statement
	for i := range b.Statements {
		s := &b.Statements[i]
		if !statementApplies(s, req, kinds, kind, from, to) {
			continue
		}
		switch s.Effect {
		case "forbid":
			forbids = append(forbids, s)
		case "require_approval":
			approvals = append(approvals, s)
		case "permit":
			permits = append(permits, s)
		}
	}
	ids := func(ss ...[]*Statement) []string {
		out := []string{}
		for _, list := range ss {
			for _, s := range list {
				out = append(out, s.ID)
			}
		}
		return out
	}
	if len(forbids) > 0 {
		return result("deny", forbids[0].ReasonCode, ids(forbids), "")
	}
	if len(approvals) == 0 && len(permits) == 0 {
		return result("deny", "DEFAULT_DENY", nil, "")
	}
	covered := map[string]bool{}
	for _, list := range [][]*Statement{approvals, permits} {
		for _, s := range list {
			for _, k := range s.ActionKinds {
				covered[k] = true
			}
		}
	}
	for k := range kinds {
		if !covered[k] {
			return result("deny", "CAPABILITY_NOT_PERMITTED", ids(approvals, permits), "")
		}
	}
	if len(approvals) > 0 {
		return result("require_approval", approvals[0].ReasonCode, ids(approvals), ApprovalRequestID(req, b.Sha))
	}
	return result("allow", permits[0].ReasonCode, ids(permits), "")
}
