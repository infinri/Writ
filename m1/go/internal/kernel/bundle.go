package kernel

import (
	"sort"

	"writ.dev/m1/proto/internal/sjson"
)

const TransitionKind = "workflow.transition"

// Statement is one policy statement of a loaded bundle.
type Statement struct {
	ID               string
	Effect           string
	ReasonCode       string
	ActionKinds      []string
	Tenants          []string // nil: any
	PrincipalRoles   []string // nil: any
	ResourcePrefixes []string // nil: any
	FromStates       []string // nil: any
	ToStates         []string // nil: no state condition
	hasTenants       bool
	hasRoles         bool
	hasPrefixes      bool
}

// Edge is a workflow transition from one state to another.
type Edge struct{ From, To string }

// Protection is the authoritative workflow_protection classification
// (D-019 N-1, N-4).
type Protection struct {
	AcceptancePolicyBundleSha string
	ProtectedStates           map[string]bool
	ProtectedEdges            map[Edge]bool
}

// IsProtected: the union of destination protection and edge protection.
func (p *Protection) IsProtected(from, to string) bool {
	return p.ProtectedStates[to] || p.ProtectedEdges[Edge{from, to}]
}

// Bundle is a policy bundle that passed load-time validation.
type Bundle struct {
	Sha        string
	Known      map[string]bool
	Issuers    map[string]bool
	Statements []Statement // sorted by statement id
	Protection *Protection // nil when the bundle has no workflow_protection
	// TransitionSupported: some statement covers workflow.transition or the
	// bundle carries workflow_protection (D-020). When true, Protection is
	// non-nil (load rejects the bundle otherwise).
	TransitionSupported bool
}

// BundleSha is the digest of the bundle content without publisher_signature.
func BundleSha(doc map[string]any) string {
	return sjson.MustDigest(sjson.Without(doc, "publisher_signature"))
}

func covers(s *Statement, kind string) bool {
	for _, k := range s.ActionKinds {
		if k == kind {
			return true
		}
	}
	return false
}

// LoadBundle validates a bundle document and returns nil if it is rejected
// (POLICY_BUNDLE_REJECTED). Pure.
func LoadBundle(v any) *Bundle {
	if !validBundleSchema(v) {
		return nil
	}
	doc := v.(map[string]any)
	b := &Bundle{Sha: BundleSha(doc), Known: map[string]bool{}, Issuers: map[string]bool{}}
	known, _ := sjson.Strings(doc["known_capabilities"])
	for _, k := range known {
		b.Known[k] = true
	}
	issuers, _ := sjson.Strings(doc["trusted_principal_issuers"])
	for _, i := range issuers {
		b.Issuers[i] = true
	}
	ids := map[string]bool{}
	stmts, _ := sjson.Arr(doc["statements"])
	for _, sv := range stmts {
		m := sv.(map[string]any)
		s := Statement{ID: m["statement_id"].(string), Effect: m["effect"].(string), ReasonCode: m["reason_code"].(string)}
		s.ActionKinds, _ = sjson.Strings(m["action_kinds"])
		if v, ok := m["tenants"]; ok {
			s.Tenants, _ = sjson.Strings(v)
			s.hasTenants = true
		}
		if v, ok := m["principal_roles"]; ok {
			s.PrincipalRoles, _ = sjson.Strings(v)
			s.hasRoles = true
		}
		if v, ok := m["resource_prefixes"]; ok {
			s.ResourcePrefixes, _ = sjson.Strings(v)
			s.hasPrefixes = true
		}
		if v, ok := m["from_states"]; ok {
			s.FromStates, _ = sjson.Strings(v)
		}
		if v, ok := m["to_states"]; ok {
			s.ToStates, _ = sjson.Strings(v)
		}
		if ids[s.ID] {
			return nil // duplicate statement_id
		}
		ids[s.ID] = true
		for _, k := range s.ActionKinds {
			if !b.Known[k] {
				return nil // statement references an unknown capability
			}
		}
		b.Statements = append(b.Statements, s)
	}
	sort.Slice(b.Statements, func(i, j int) bool { return b.Statements[i].ID < b.Statements[j].ID })

	anyTransition := false
	for i := range b.Statements {
		s := &b.Statements[i]
		if !covers(s, TransitionKind) {
			continue
		}
		anyTransition = true
		// A permit or require_approval transition statement must name its
		// target states (D-017: unscoped transition permits are rejected).
		if s.Effect != "forbid" && s.ToStates == nil {
			return nil
		}
	}
	wv, hasProtection := doc["workflow_protection"]
	b.TransitionSupported = anyTransition || hasProtection
	if hasProtection {
		w := wv.(map[string]any)
		p := &Protection{
			AcceptancePolicyBundleSha: w["acceptance_policy_bundle_sha"].(string),
			ProtectedStates:           map[string]bool{},
			ProtectedEdges:            map[Edge]bool{},
		}
		states, _ := sjson.Strings(w["protected_to_states"])
		for _, s := range states {
			if p.ProtectedStates[s] {
				return nil // duplicated protected state
			}
			p.ProtectedStates[s] = true
		}
		edges, _ := sjson.Arr(w["protected_transitions"])
		for _, ev := range edges {
			e := ev.(map[string]any)
			edge := Edge{e["from_state"].(string), e["to_state"].(string)}
			if edge.From == edge.To || p.ProtectedEdges[edge] {
				return nil // self edge or duplicated edge
			}
			p.ProtectedEdges[edge] = true
		}
		b.Protection = p
	} else if b.TransitionSupported {
		return nil // support declared without a classification (D-019 N-1, D-020)
	}
	if b.Protection != nil && bypassable(b) {
		return nil
	}
	return b
}

// bypassable reports whether some protected edge A->B (B not a protected
// state) is reachable from A by transitions permitted or approvable by
// statements that use no protected edge and enter no protected state
// (D-019 N-4). A statement without from_states permits any source; the state
// universe is every state named anywhere in the bundle.
func bypassable(b *Bundle) bool {
	p := b.Protection
	universe := map[string]bool{}
	for s := range p.ProtectedStates {
		universe[s] = true
	}
	for e := range p.ProtectedEdges {
		universe[e.From] = true
		universe[e.To] = true
	}
	for i := range b.Statements {
		for _, s := range b.Statements[i].FromStates {
			universe[s] = true
		}
		for _, s := range b.Statements[i].ToStates {
			universe[s] = true
		}
	}
	all := make([]string, 0, len(universe))
	for s := range universe {
		all = append(all, s)
	}
	adj := map[string][]string{}
	for i := range b.Statements {
		s := &b.Statements[i]
		if s.Effect == "forbid" || !covers(s, TransitionKind) {
			continue
		}
		sources := s.FromStates
		if sources == nil {
			sources = all
		}
		for _, from := range sources {
			for _, to := range s.ToStates {
				if from == to || p.ProtectedStates[to] || p.ProtectedEdges[Edge{from, to}] {
					continue
				}
				adj[from] = append(adj[from], to)
			}
		}
	}
	for e := range p.ProtectedEdges {
		if p.ProtectedStates[e.To] {
			continue
		}
		seen := map[string]bool{e.From: true}
		queue := []string{e.From}
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]
			for _, nxt := range adj[cur] {
				if nxt == e.To {
					return true
				}
				if !seen[nxt] {
					seen[nxt] = true
					queue = append(queue, nxt)
				}
			}
		}
	}
	return false
}
