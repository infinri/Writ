package transition

import "writ.dev/m1/proto/internal/sjson"

// MaxConcurrentCount bounds promote_concurrent `count` (implementation limit;
// a larger count is INPUT_MALFORMED rather than an out-of-memory crash).
const MaxConcurrentCount = 65536

var requiredStrings = []string{
	"tenant_id", "project_id", "task_id", "workflow_id", "state",
	"artifact_snapshot_id", "artifact_digest",
	"acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha",
	"authorization_bundle_sha", "now",
}

// faultNames is the transition-scenario fault vocabulary (SCENARIOS.md).
// A `fault` on promote or effect must be one of these names, else the
// scenario is INPUT_MALFORMED; a known name at a point the operation does not
// have (for example crash_after_dispatch on promote) never fires.
var faultNames = map[string]bool{
	"crash_before_commit": true, "crash_before_reserve": true, "crash_after_dispatch": true,
}

var takesFault = map[string]bool{"promote": true, "effect": true}

// wellFormed reports whether a scenario is structurally usable
// (M1-PARITY-SUITE, protocol clarifications): otherwise the whole scenario is
// INPUT_MALFORMED and nothing runs.
func wellFormed(initial map[string]any, steps []any) bool {
	for _, k := range requiredStrings {
		if _, ok := sjson.Str(initial, k); !ok {
			return false
		}
	}
	if rev, ok := sjson.Int(initial, "revision"); !ok || rev < 0 {
		return false
	}
	if _, ok := initial["acceptance_requirements"]; !ok {
		return false
	}
	for _, k := range []string{"consumed_record_ids", "revoked_evaluator_ids"} {
		if v, present := initial[k]; present {
			if _, ok := sjson.Strings(v); !ok {
				return false
			}
		}
	}
	if v, present := initial["effect_prerequisites"]; present {
		if _, ok := sjson.Obj(v); !ok {
			return false
		}
	}
	if v, present := initial["last_promotion"]; present && v != nil {
		if _, ok := sjson.Obj(v); !ok {
			return false
		}
	}
	return opsWellFormed(steps)
}

func opsWellFormed(ops []any) bool {
	for _, o := range ops {
		op, ok := sjson.Obj(o)
		if !ok || !opWellFormed(op) {
			return false
		}
	}
	return true
}

func opWellFormed(op map[string]any) bool {
	name, _ := sjson.Str(op, "op")
	if f, present := op["fault"]; present && takesFault[name] {
		if s, ok := f.(string); !ok || !faultNames[s] {
			return false
		}
	}
	switch name {
	case "concurrent":
		ops, ok := sjson.Arr(op["operations"])
		return ok && opsWellFormed(ops)
	case "promote_concurrent":
		n, ok := sjson.Int(op, "count")
		if !ok || n < 1 || n > MaxConcurrentCount {
			return false
		}
		tmpl, ok := sjson.Obj(op["promote"])
		if !ok {
			return false
		}
		tmpl = sjson.Without(tmpl)
		tmpl["op"] = "promote"
		return opWellFormed(tmpl)
	}
	return true
}
