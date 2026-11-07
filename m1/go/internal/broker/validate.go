package broker

import (
	"writ.dev/m1/proto/internal/kernel"
	"writ.dev/m1/proto/internal/sjson"
)

var executeFaults = map[string]bool{
	"crash_before_reserve": true, "crash_after_reserve": true, "crash_after_dispatch": true, "effect_fails": true,
}

// Dependency names other than policy and ledger are accepted and have no
// effect; only the status vocabulary is checked.
var dependencyStatus = map[string]bool{"up": true, "down": true}

// wellFormed reports whether a scenario is structurally usable
// (M1-PARITY-SUITE, protocol clarifications): otherwise the whole scenario is
// INPUT_MALFORMED and nothing runs.
func wellFormed(initial map[string]any, steps []any) bool {
	if _, ok := kernel.ParseTime(sjson.StrOr(initial, "now")); !ok {
		return false
	}
	if rev, ok := sjson.Int(initial, "state_revision"); !ok || rev < 0 {
		return false
	}
	if _, ok := initial["bundle"]; !ok {
		return false
	}
	if _, ok := sjson.Str(initial, "permit_key"); !ok {
		return false
	}
	for _, k := range []string{"ingress_keys", "channels"} {
		if _, ok := sjson.Obj(initial[k]); !ok {
			return false
		}
	}
	if v, present := initial["dependencies"]; present {
		deps, ok := sjson.Obj(v)
		if !ok {
			return false
		}
		for _, s := range deps {
			if st, ok := s.(string); !ok || !dependencyStatus[st] {
				return false
			}
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
	if f, present := op["fault"]; present && name == "execute" {
		if s, ok := f.(string); !ok || !executeFaults[s] {
			return false
		}
	}
	switch name {
	case "concurrent":
		ops, ok := sjson.Arr(op["operations"])
		return ok && opsWellFormed(ops)
	case "set_dependency":
		_, okN := sjson.Str(op, "name")
		s, okS := sjson.Str(op, "status")
		return okN && okS && dependencyStatus[s]
	}
	return true
}
