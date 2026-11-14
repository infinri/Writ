// Package acceptance is the pure check_acceptance function
// (m1/fixtures/acceptance/SEMANTICS.md, m1-draft-3). It verifies no signature
// or issuance (boundary 2); record_digest is an unkeyed consistency check.
package acceptance

import (
	"regexp"
	"sort"
	"time"

	"writ.dev/m1/proto/internal/sjson"
)

const ContractVersion = "m1-draft-3"

var (
	lowerID    = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)    // criterion, workflow, state, evaluator
	scopeID    = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`) // tenant, project, task, record, snapshot, policy
	versionPat = regexp.MustCompile(`^[0-9]{1,9}$`)
	digestPat  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	timePat    = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$`)
)

const timeLayout = "2006-01-02T15:04:05Z"

// ParseTimestamp parses the strict Writ timestamp form with a valid calendar time.
func ParseTimestamp(s string) (time.Time, bool) {
	if !timePat.MatchString(s) {
		return time.Time{}, false
	}
	t, err := time.Parse(timeLayout, s)
	return t, err == nil
}

type shape struct {
	m  map[string]any
	ok bool
}

func newShape(v any, keys ...string) *shape {
	m, ok := sjson.Obj(v)
	sh := &shape{m: m, ok: ok}
	if !ok {
		return sh
	}
	if len(m) != len(keys) {
		sh.ok = false
	}
	for _, k := range keys {
		if !sjson.Has(m, k) {
			sh.ok = false
		}
	}
	return sh
}

func (s *shape) pat(k string, re *regexp.Regexp) string {
	v, ok := s.m[k].(string)
	if !ok || !re.MatchString(v) {
		s.ok = false
	}
	return v
}

func (s *shape) enum(k string, allowed ...string) string {
	v, ok := s.m[k].(string)
	if !ok {
		s.ok = false
		return ""
	}
	for _, a := range allowed {
		if v == a {
			return v
		}
	}
	s.ok = false
	return v
}

func (s *shape) ts(k string) time.Time {
	v, ok := s.m[k].(string)
	if !ok {
		s.ok = false
		return time.Time{}
	}
	t, ok := ParseTimestamp(v)
	if !ok {
		s.ok = false
	}
	return t
}

func (s *shape) revision(k string) int64 {
	n, ok := s.m[k].(int64)
	if !ok || n < 0 || n > sjson.MaxSafeInteger {
		s.ok = false
	}
	return n
}

func (s *shape) ids(k string, re *regexp.Regexp) map[string]bool {
	out := map[string]bool{}
	ss, ok := sjson.Strings(s.m[k])
	if !ok {
		s.ok = false
		return out
	}
	for _, id := range ss {
		if !re.MatchString(id) {
			s.ok = false
		}
		out[id] = true
	}
	return out
}

type transition struct{ from, to string }

func (s *shape) transition(k string) transition {
	t := newShape(s.m[k], "from_state", "to_state")
	if !t.ok {
		s.ok = false
		return transition{}
	}
	tr := transition{t.pat("from_state", lowerID), t.pat("to_state", lowerID)}
	if !t.ok {
		s.ok = false
	}
	return tr
}

type binding struct {
	tenant, project, task, workflow string
	tr                              transition
	snapshot, artifactDigest        string
	policyID, policyVersion, bundle string
	revision                        int64
}

func (s *shape) binding() binding {
	return binding{
		tenant: s.pat("tenant_id", scopeID), project: s.pat("project_id", scopeID),
		task: s.pat("task_id", scopeID), workflow: s.pat("workflow_id", lowerID),
		tr:       s.transition("transition"),
		snapshot: s.pat("artifact_snapshot_id", scopeID), artifactDigest: s.pat("artifact_digest", digestPat),
		policyID: s.pat("acceptance_policy_id", scopeID), policyVersion: s.pat("acceptance_policy_version", versionPat),
		bundle:   s.pat("acceptance_policy_bundle_sha", digestPat),
		revision: s.revision("state_revision"),
	}
}

type criterion struct {
	id, class, status, evaluator, trust string
	mandatory                           bool
	freshUntil                          time.Time
}

// Result is {"ok": true} or {"refusal": code}.
func Result(code string) map[string]any {
	if code == "" {
		return map[string]any{"ok": true}
	}
	return map[string]any{"refusal": code}
}

var classTrust = map[string]string{"mechanical": "M", "evidence": "E", "human": "H"}

// Check returns "" when the record satisfies the requirements in the current
// state, or the first refusal code in the normative order. All inputs are
// parsed values; record is untrusted.
func Check(record, current, requirements any) string {
	// 1. current
	cur := newShape(current, "tenant_id", "project_id", "task_id", "workflow_id", "transition",
		"artifact_snapshot_id", "artifact_digest", "acceptance_policy_id", "acceptance_policy_version",
		"acceptance_policy_bundle_sha", "state_revision", "now", "consumed_record_ids", "revoked_evaluator_ids")
	var cb binding
	var now time.Time
	var consumed, revoked map[string]bool
	if cur.ok {
		cb = cur.binding()
		now = cur.ts("now")
		consumed = cur.ids("consumed_record_ids", scopeID)
		revoked = cur.ids("revoked_evaluator_ids", lowerID)
	}
	if !cur.ok {
		return "CURRENT_STATE_MALFORMED"
	}

	// 2. requirements
	req := newShape(requirements, "acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha", "criteria")
	type requirement struct{ id, class string }
	var reqs []requirement
	var rPolicy, rVersion, rBundle string
	if req.ok {
		rPolicy = req.pat("acceptance_policy_id", scopeID)
		rVersion = req.pat("acceptance_policy_version", versionPat)
		rBundle = req.pat("acceptance_policy_bundle_sha", digestPat)
		list, ok := sjson.Arr(req.m["criteria"])
		if !ok || len(list) == 0 {
			req.ok = false
		}
		seen := map[string]bool{}
		for _, e := range list {
			c := newShape(e, "criterion_id", "required_class")
			if !c.ok {
				req.ok = false
				break
			}
			r := requirement{c.pat("criterion_id", lowerID), c.enum("required_class", "mechanical", "evidence", "human")}
			if !c.ok || seen[r.id] {
				req.ok = false
				break
			}
			seen[r.id] = true
			reqs = append(reqs, r)
		}
	}
	if !req.ok {
		return "REQUIREMENTS_MALFORMED"
	}
	sort.Slice(reqs, func(i, j int) bool { return reqs[i].id < reqs[j].id })

	// 3. requirements bound to the active policy
	if rPolicy != cb.policyID || rVersion != cb.policyVersion || rBundle != cb.bundle {
		return "REQUIREMENTS_POLICY_MISMATCH"
	}

	// 4. record shape
	rec := newShape(record, "contract_version", "acceptance_record_id", "tenant_id", "project_id", "task_id",
		"workflow_id", "transition", "artifact_snapshot_id", "artifact_digest", "acceptance_policy_id",
		"acceptance_policy_version", "acceptance_policy_bundle_sha", "state_revision", "status", "issued_at",
		"expires_at", "criteria", "record_digest")
	var rb binding
	var recordID, status, recordDigest string
	var issued, expires time.Time
	crits := map[string]*criterion{}
	var critList []*criterion
	if rec.ok {
		rec.enum("contract_version", ContractVersion)
		recordID = rec.pat("acceptance_record_id", scopeID)
		rb = rec.binding()
		status = rec.enum("status", "accepted", "rejected", "incomplete", "needs_human_review")
		issued = rec.ts("issued_at")
		expires = rec.ts("expires_at")
		recordDigest = rec.pat("record_digest", digestPat)
		list, ok := sjson.Arr(rec.m["criteria"])
		if !ok || len(list) == 0 {
			rec.ok = false
		}
		for _, e := range list {
			c := newShape(e, "criterion_id", "mandatory", "required_class", "status", "evaluator_id", "evaluator_trust", "fresh_until")
			if !c.ok {
				rec.ok = false
				break
			}
			cr := &criterion{
				id:        c.pat("criterion_id", lowerID),
				class:     c.enum("required_class", "mechanical", "evidence", "human", "model_assisted"),
				status:    c.enum("status", "pass", "fail", "unknown", "stale", "waived"),
				evaluator: c.pat("evaluator_id", lowerID),
				trust:     c.enum("evaluator_trust", "U", "MA", "M", "E", "H"),
			}
			mand, isBool := c.m["mandatory"].(bool)
			cr.mandatory = mand
			cr.freshUntil = c.ts("fresh_until")
			if !isBool || !c.ok || crits[cr.id] != nil || (cr.class == "model_assisted" && cr.mandatory) {
				rec.ok = false
				break
			}
			crits[cr.id] = cr
			critList = append(critList, cr)
		}
	}
	if !rec.ok {
		return "ACCEPTANCE_RECORD_MALFORMED"
	}

	// 5. integrity and validity window
	recomputed, err := sjson.Digest(sjson.Without(rec.m, "record_digest"))
	if err != nil || recomputed != recordDigest || !expires.After(issued) || issued.After(now) {
		return "ACCEPTANCE_RECORD_INVALID"
	}
	// 6. replay
	if consumed[recordID] {
		return "ACCEPTANCE_REPLAYED"
	}
	// 7..11 binding
	if rb.tenant != cb.tenant || rb.project != cb.project || rb.task != cb.task || rb.workflow != cb.workflow {
		return "ACCEPTANCE_SCOPE_MISMATCH"
	}
	if rb.tr != cb.tr {
		return "ACCEPTANCE_TRANSITION_MISMATCH"
	}
	if rb.snapshot != cb.snapshot || rb.artifactDigest != cb.artifactDigest {
		return "ACCEPTANCE_ARTIFACT_MISMATCH"
	}
	if rb.policyID != cb.policyID || rb.policyVersion != cb.policyVersion || rb.bundle != cb.bundle {
		return "ACCEPTANCE_POLICY_MISMATCH"
	}
	if rb.revision != cb.revision {
		return "ACCEPTANCE_REVISION_MISMATCH"
	}
	// 12. expiry
	if !now.Before(expires) {
		return "ACCEPTANCE_EXPIRED"
	}
	// 13. completeness against the trusted requirement set
	required := map[string]bool{}
	for _, r := range reqs {
		required[r.id] = true
		c := crits[r.id]
		if c == nil {
			return "ACCEPTANCE_CRITERIA_MISSING"
		}
		if !c.mandatory || c.class != r.class {
			return "ACCEPTANCE_CRITERIA_MISMATCH"
		}
	}
	for _, c := range critList {
		if c.mandatory && !required[c.id] {
			return "ACCEPTANCE_CRITERIA_MISMATCH"
		}
	}
	// 14. per required criterion, ascending id
	for _, r := range reqs {
		c := crits[r.id]
		switch {
		case c.trust != classTrust[r.class]:
			return "ACCEPTANCE_UNTRUSTED_EVALUATOR"
		case revoked[c.evaluator]:
			return "ACCEPTANCE_EVALUATOR_REVOKED"
		case c.status == "stale" || (c.status == "pass" && !now.Before(c.freshUntil)):
			return "ACCEPTANCE_STALE"
		case c.status == "fail" || c.status == "unknown" || c.status == "waived":
			return "ACCEPTANCE_INCOMPLETE"
		}
	}
	// 15. record status consistent
	if status != "accepted" {
		return "ACCEPTANCE_RECORD_INVALID"
	}
	return ""
}
