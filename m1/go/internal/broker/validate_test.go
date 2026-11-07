package broker

import (
	"bufio"
	"os"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

func fixtureCases(t *testing.T) []map[string]any {
	t.Helper()
	f, err := os.Open("../../../fixtures/sets/broker-scenarios.jsonl")
	if err != nil {
		t.Skip("fixtures not available:", err)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	var out []map[string]any
	for sc.Scan() {
		v, perr := sjson.ParseUnlimited(sc.Text())
		if perr != nil {
			t.Fatal(perr)
		}
		out = append(out, v.(map[string]any))
	}
	return out
}

// approvalCase returns the input of the first fixture with an issued grant,
// and the index of the approve step that issued it.
func approvalCase(t *testing.T) (map[string]any, int) {
	t.Helper()
	for _, c := range fixtureCases(t) {
		in, ok := c["input"].(map[string]any)
		if !ok {
			continue
		}
		out := Run(sjson.Clone(in))
		steps, _ := out["steps"].([]any)
		for i, s := range steps {
			if s.(map[string]any)["result"] == "grant_issued" {
				return sjson.Clone(in).(map[string]any), i
			}
		}
	}
	t.Fatal("no fixture issues a grant")
	return nil, 0
}

func malformed(out map[string]any) bool {
	return out["input_error"] == "INPUT_MALFORMED" && len(out) == 1
}

func TestInputMalformed(t *testing.T) {
	base, _ := approvalCase(t)
	ini := func(in map[string]any) map[string]any { return in["initial"].(map[string]any) }
	mutations := map[string]func(in map[string]any){
		"no steps":               func(in map[string]any) { delete(in, "steps") },
		"steps not array":        func(in map[string]any) { in["steps"] = "x" },
		"initial empty":          func(in map[string]any) { in["initial"] = map[string]any{} },
		"no now":                 func(in map[string]any) { delete(ini(in), "now") },
		"bad now":                func(in map[string]any) { ini(in)["now"] = "yesterday" },
		"no state_revision":      func(in map[string]any) { delete(ini(in), "state_revision") },
		"no bundle":              func(in map[string]any) { delete(ini(in), "bundle") },
		"no permit_key":          func(in map[string]any) { delete(ini(in), "permit_key") },
		"channels not obj":       func(in map[string]any) { ini(in)["channels"] = []any{} },
		"unknown dep status":     func(in map[string]any) { ini(in)["dependencies"] = map[string]any{"ledger": "sideways"} },
		"step not object":        func(in map[string]any) { in["steps"] = append(in["steps"].([]any), "x") },
		"unknown dep in initial": func(in map[string]any) { ini(in)["dependencies"] = map[string]any{"graph": "up"} },
		"set_dependency bad status": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "set_dependency", "name": "ledger", "status": "sideways"}}
		},
		"execute unknown fault": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "execute", "fault": "zz_fault"}}
		},
		"nested non-object": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "concurrent", "operations": []any{map[string]any{"op": "concurrent", "operations": []any{int64(1)}}}}}
		},
	}
	for name, mut := range mutations {
		in := sjson.Clone(base).(map[string]any)
		mut(in)
		if out := Run(in); !malformed(out) {
			t.Errorf("%s: got %s", name, sjson.Encode(out))
		}
	}
}

func lastStep(out map[string]any) string {
	steps := out["steps"].([]any)
	return string(sjson.Encode(steps[len(steps)-1]))
}

// approve with a grant_ref already in use is refused and never resets uses.
func TestGrantRefInUse(t *testing.T) {
	in, i := approvalCase(t)
	steps := in["steps"].([]any)
	before := Run(sjson.Clone(in))
	in["steps"] = append(steps, sjson.Clone(steps[i]))
	out := Run(in)
	if got := lastStep(out); got != `{"code":"GRANT_REF_IN_USE","result":"refused"}` {
		t.Fatalf("re-approve: %s", got)
	}
	bf, af := before["final"].(map[string]any), out["final"].(map[string]any)
	if string(sjson.Encode(bf["grant_uses"])) != string(sjson.Encode(af["grant_uses"])) ||
		af["refusals"] != bf["refusals"].(int64)+1 {
		t.Fatalf("final changed: %s -> %s", sjson.Encode(bf), sjson.Encode(af))
	}
}

// present_permit naming a step that issued no permit is PERMIT_MISSING;
// an unknown op is OPERATION_UNKNOWN.
func TestPermitMissingAndUnknownOp(t *testing.T) {
	in, i := approvalCase(t)
	steps := in["steps"].([]any)
	for _, op := range []map[string]any{
		{"op": "present_permit", "of_step": int64(i)},
		{"op": "present_permit", "of_step": int64(-1)},
		{"op": "present_permit", "of_step": int64(len(steps) + 5)},
		{"op": "present_permit"},
		{"op": "present_permit", "of_step": "0"},
	} {
		in["steps"] = append(sjson.Clone(steps).([]any), op)
		if got := lastStep(Run(sjson.Clone(in))); got != `{"code":"PERMIT_MISSING","result":"refused"}` {
			t.Errorf("%s: %s", sjson.Encode(op), got)
		}
	}
	in["steps"] = append(sjson.Clone(steps).([]any), map[string]any{"op": "zz_unknown_operation"})
	if got := lastStep(Run(in)); got != `{"code":"OPERATION_UNKNOWN","result":"refused"}` {
		t.Errorf("unknown op: %s", got)
	}
}

// D-021 (9): the declared broker dependencies are exactly policy and ledger;
// any other name, even with a valid status, makes the whole scenario
// INPUT_MALFORMED with no step run. Declared names stay accepted.
func TestUndeclaredDependencyName(t *testing.T) {
	base, _ := approvalCase(t)
	steps := base["steps"].([]any)
	for name, mut := range map[string]func(in map[string]any){
		"set_dependency unknown name": func(in map[string]any) {
			in["steps"] = append([]any{map[string]any{"op": "set_dependency", "name": "graph", "status": "down"}}, sjson.Clone(steps).([]any)...)
		},
		"set_dependency name not string": func(in map[string]any) {
			in["steps"] = append(sjson.Clone(steps).([]any), map[string]any{"op": "set_dependency", "name": int64(1), "status": "up"})
		},
		"nested set_dependency unknown name": func(in map[string]any) {
			in["steps"] = append(sjson.Clone(steps).([]any), map[string]any{"op": "concurrent", "operations": []any{
				map[string]any{"op": "set_dependency", "name": "cache", "status": "up"}}})
		},
		"initial extra dependency": func(in map[string]any) {
			in["initial"].(map[string]any)["dependencies"] = map[string]any{"policy": "up", "ledger": "up", "cache": "up"}
		},
		"initial dependency case differs": func(in map[string]any) {
			in["initial"].(map[string]any)["dependencies"] = map[string]any{"Policy": "up"}
		},
	} {
		in := sjson.Clone(base).(map[string]any)
		mut(in)
		if out := Run(in); !malformed(out) {
			t.Errorf("%s: got %s", name, sjson.Encode(out))
		}
	}
	for _, dep := range []string{"policy", "ledger"} {
		in := sjson.Clone(base).(map[string]any)
		in["initial"].(map[string]any)["dependencies"] = map[string]any{dep: "up"}
		in["steps"] = append(sjson.Clone(steps).([]any), map[string]any{"op": "set_dependency", "name": dep, "status": "up"})
		if out := Run(in); malformed(out) {
			t.Errorf("declared %s rejected", dep)
		}
	}
}
