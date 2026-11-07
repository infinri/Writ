package kernel

import (
	"regexp"
	"time"
	"unicode/utf8"

	"writ.dev/m1/proto/internal/sjson"
)

// Hand-written validators for docs/contracts/action-request.schema.json and
// docs/contracts/policy-bundle.schema.json (the subset of JSON Schema those
// two documents use). They report only valid or invalid, which is all the
// decision needs (REQUEST_SCHEMA_INVALID, POLICY_BUNDLE_REJECTED).

var (
	kindPattern    = regexp.MustCompile(`^[a-z][a-z0-9_.-]*$`)
	reasonPattern  = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
	StatePattern   = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)
	digestPattern  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	rfc3339Pattern = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$`)
)

// ParseTime parses a strict RFC 3339 date-time (valid calendar date and time).
func ParseTime(s string) (time.Time, bool) {
	if !rfc3339Pattern.MatchString(s) {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339Nano, normalizeRFC3339(s))
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func normalizeRFC3339(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c == 't' || c == 'z' {
			b[i] = c - 'a' + 'A'
		}
	}
	return string(b)
}

type check struct{ ok bool }

func (c *check) fail()          { c.ok = false }
func (c *check) need(cond bool) { c.ok = c.ok && cond }

func strLen(s string) int { return utf8.RuneCountInString(s) }

func closed(c *check, m map[string]any, allowed ...string) {
	set := map[string]bool{}
	for _, a := range allowed {
		set[a] = true
	}
	for k := range m {
		if !set[k] {
			c.fail()
		}
	}
}

func required(c *check, m map[string]any, keys ...string) {
	for _, k := range keys {
		if !sjson.Has(m, k) {
			c.fail()
		}
	}
}

// optString: absent, or a string (or null when nullable) with a length range.
func strField(c *check, m map[string]any, k string, nullable bool, minLen, maxLen int) {
	v, present := m[k]
	if !present {
		return
	}
	if v == nil {
		c.need(nullable)
		return
	}
	s, ok := v.(string)
	if !ok {
		c.fail()
		return
	}
	n := strLen(s)
	c.need(n >= minLen && (maxLen < 0 || n <= maxLen))
}

// stringArray: array of strings, optionally unique, minimum items, item check.
func stringArray(c *check, v any, unique bool, minItems int, item func(string) bool) []string {
	ss, ok := sjson.Strings(v)
	if !ok {
		c.fail()
		return nil
	}
	c.need(len(ss) >= minItems)
	if unique {
		seen := map[string]bool{}
		for _, s := range ss {
			if seen[s] {
				c.fail()
			}
			seen[s] = true
		}
	}
	if item != nil {
		for _, s := range ss {
			c.need(item(s))
		}
	}
	return ss
}

func minLen1(s string) bool { return strLen(s) >= 1 }

// ValidRequest validates an ActionRequest against action-request.schema.json.
func ValidRequest(v any) bool {
	c := &check{ok: true}
	m, ok := sjson.Obj(v)
	if !ok {
		return false
	}
	closed(c, m, "contract_version", "request_id", "trace_id", "tenant_id", "principal", "project_id",
		"session_id", "agent_id", "parent_session_id", "idempotency_key", "expected_state_revision",
		"policy_bundle_sha", "deadline_at", "action")
	required(c, m, "contract_version", "request_id", "trace_id", "tenant_id", "principal", "project_id",
		"session_id", "idempotency_key", "action")
	if !c.ok {
		return false
	}
	c.need(m["contract_version"] == "2.0")
	strField(c, m, "request_id", false, 1, 128)
	strField(c, m, "trace_id", false, 1, 128)
	strField(c, m, "tenant_id", false, 1, 128)
	strField(c, m, "project_id", false, 1, -1)
	strField(c, m, "session_id", false, 1, -1)
	strField(c, m, "agent_id", true, 0, -1)
	strField(c, m, "parent_session_id", true, 0, -1)
	strField(c, m, "idempotency_key", false, 1, 256)
	strField(c, m, "policy_bundle_sha", true, 0, -1)
	if v, ok := m["expected_state_revision"]; ok && v != nil {
		n, isInt := v.(int64)
		c.need(isInt && n >= 0 && n <= sjson.MaxSafeInteger)
	}
	if v, ok := m["deadline_at"]; ok && v != nil {
		s, isStr := v.(string)
		c.need(isStr)
		if isStr {
			_, valid := ParseTime(s)
			c.need(valid)
		}
	}
	p, ok := sjson.Obj(m["principal"])
	if !ok {
		return false
	}
	closed(c, p, "id", "verified_by", "authn_method", "roles")
	required(c, p, "id", "verified_by", "authn_method")
	strField(c, p, "id", false, 1, -1)
	strField(c, p, "verified_by", false, 1, -1)
	strField(c, p, "authn_method", false, 1, -1)
	if r, ok := p["roles"]; ok {
		stringArray(c, r, true, 0, nil)
	}
	a, ok := sjson.Obj(m["action"])
	if !ok {
		return false
	}
	closed(c, a, "kind", "resource", "action_digest", "attributes", "requested_capabilities")
	required(c, a, "kind", "resource", "action_digest")
	if !c.ok {
		return false
	}
	kind, isStr := a["kind"].(string)
	c.need(isStr && kindPattern.MatchString(kind))
	strField(c, a, "action_digest", false, 16, -1)
	if at, ok := a["attributes"]; ok {
		_, isObj := sjson.Obj(at)
		c.need(isObj)
	}
	if rc, ok := a["requested_capabilities"]; ok {
		stringArray(c, rc, true, 0, nil)
	}
	r, ok := sjson.Obj(a["resource"])
	if !ok {
		return false
	}
	closed(c, r, "uri", "canonical_resource_id")
	required(c, r, "uri")
	strField(c, r, "uri", false, 1, -1)
	strField(c, r, "canonical_resource_id", true, 0, -1)
	return c.ok
}

// validBundleSchema validates a bundle against policy-bundle.schema.json.
func validBundleSchema(v any) bool {
	c := &check{ok: true}
	m, ok := sjson.Obj(v)
	if !ok {
		return false
	}
	closed(c, m, "contract_version", "bundle_id", "version", "publisher", "description",
		"known_capabilities", "trusted_principal_issuers", "statements", "publisher_signature",
		"workflow_protection")
	required(c, m, "contract_version", "bundle_id", "version", "publisher", "known_capabilities",
		"trusted_principal_issuers", "statements")
	if !c.ok {
		return false
	}
	c.need(m["contract_version"] == "2.0")
	strField(c, m, "bundle_id", false, 1, -1)
	strField(c, m, "version", false, 1, -1)
	strField(c, m, "publisher", false, 1, -1)
	strField(c, m, "description", true, 0, -1)
	strField(c, m, "publisher_signature", true, 0, -1)
	stringArray(c, m["known_capabilities"], true, 1, kindPattern.MatchString)
	stringArray(c, m["trusted_principal_issuers"], true, 1, minLen1)
	stmts, ok := sjson.Arr(m["statements"])
	if !ok {
		return false
	}
	for _, sv := range stmts {
		s, ok := sjson.Obj(sv)
		if !ok {
			return false
		}
		closed(c, s, "statement_id", "effect", "tenants", "principal_roles", "action_kinds",
			"resource_prefixes", "reason_code", "from_states", "to_states")
		required(c, s, "statement_id", "effect", "action_kinds", "reason_code")
		if !c.ok {
			return false
		}
		strField(c, s, "statement_id", false, 1, -1)
		eff, _ := s["effect"].(string)
		c.need(eff == "forbid" || eff == "require_approval" || eff == "permit")
		rc, isStr := s["reason_code"].(string)
		c.need(isStr && reasonPattern.MatchString(rc))
		stringArray(c, s["action_kinds"], true, 1, minLen1)
		for _, k := range []string{"tenants", "principal_roles", "resource_prefixes"} {
			if v, ok := s[k]; ok {
				stringArray(c, v, true, 0, minLen1)
			}
		}
		for _, k := range []string{"from_states", "to_states"} {
			if v, ok := s[k]; ok {
				stringArray(c, v, true, 1, StatePattern.MatchString)
			}
		}
	}
	if wv, ok := m["workflow_protection"]; ok {
		w, ok := sjson.Obj(wv)
		if !ok {
			return false
		}
		closed(c, w, "acceptance_policy_bundle_sha", "protected_to_states", "protected_transitions")
		required(c, w, "acceptance_policy_bundle_sha", "protected_to_states", "protected_transitions")
		if !c.ok {
			return false
		}
		d, isStr := w["acceptance_policy_bundle_sha"].(string)
		c.need(isStr && digestPattern.MatchString(d))
		stringArray(c, w["protected_to_states"], false, 0, StatePattern.MatchString)
		edges, ok := sjson.Arr(w["protected_transitions"])
		if !ok {
			return false
		}
		for _, ev := range edges {
			e, ok := sjson.Obj(ev)
			if !ok {
				return false
			}
			closed(c, e, "from_state", "to_state")
			required(c, e, "from_state", "to_state")
			for _, k := range []string{"from_state", "to_state"} {
				s, isStr := e[k].(string)
				c.need(isStr && StatePattern.MatchString(s))
			}
		}
	}
	return c.ok
}
