package transition

import (
	"bufio"
	"os"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

// fixtureInput returns a deep copy of a frozen fixture case's input.
func fixtureInput(t *testing.T, id string) map[string]any {
	t.Helper()
	f, err := os.Open("../../../fixtures/sets/transition-scenarios.jsonl")
	if err != nil {
		t.Skip("fixtures not available:", err)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		v, perr := sjson.ParseUnlimited(sc.Text())
		if perr != nil {
			t.Fatal(perr)
		}
		c := v.(map[string]any)
		if c["id"] == id {
			return sjson.Clone(c["input"]).(map[string]any)
		}
	}
	t.Fatalf("fixture %s not found", id)
	return nil
}

func malformed(out map[string]any) bool {
	return out["input_error"] == "INPUT_MALFORMED" && len(out) == 1
}

// Protocol clarifications (M1-05): structurally unusable scenarios are
// INPUT_MALFORMED as a whole and never crash.
func TestInputMalformed(t *testing.T) {
	base := fixtureInput(t, "sc-10-race-16")
	if out := Run(sjson.Clone(base)); malformed(out) {
		t.Fatal("fixture input reported malformed")
	}
	firstStep := func(in map[string]any) map[string]any { return in["steps"].([]any)[0].(map[string]any) }
	mutations := map[string]func(in map[string]any){
		"no steps":            func(in map[string]any) { delete(in, "steps") },
		"steps not array":     func(in map[string]any) { in["steps"] = map[string]any{} },
		"no initial":          func(in map[string]any) { delete(in, "initial") },
		"initial not object":  func(in map[string]any) { in["initial"] = []any{} },
		"initial empty":       func(in map[string]any) { in["initial"] = map[string]any{} },
		"no tenant":           func(in map[string]any) { delete(in["initial"].(map[string]any), "tenant_id") },
		"no requirements":     func(in map[string]any) { delete(in["initial"].(map[string]any), "acceptance_requirements") },
		"revision string":     func(in map[string]any) { in["initial"].(map[string]any)["revision"] = "7" },
		"revision negative":   func(in map[string]any) { in["initial"].(map[string]any)["revision"] = int64(-1) },
		"consumed not list":   func(in map[string]any) { in["initial"].(map[string]any)["consumed_record_ids"] = "x" },
		"step not object":     func(in map[string]any) { in["steps"] = append(in["steps"].([]any), int64(1)) },
		"count negative":      func(in map[string]any) { firstStep(in)["count"] = int64(-1) },
		"count zero":          func(in map[string]any) { firstStep(in)["count"] = int64(0) },
		"count string":        func(in map[string]any) { firstStep(in)["count"] = "16" },
		"count missing":       func(in map[string]any) { delete(firstStep(in), "count") },
		"count too large":     func(in map[string]any) { firstStep(in)["count"] = sjson.MaxSafeInteger },
		"template not object": func(in map[string]any) { firstStep(in)["promote"] = nil },
		"template bad fault":  func(in map[string]any) { firstStep(in)["promote"].(map[string]any)["fault"] = "zz" },
		"unknown promote fault": func(in map[string]any) {
			p := sjson.Clone(firstStep(in)["promote"]).(map[string]any)
			p["op"], p["fault"] = "promote", "zz_fault"
			in["steps"] = []any{p}
		},
		"fault not string": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "effect", "idempotency_key": "e", "fault": int64(1)}}
		},
		"operations not array": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "concurrent", "operations": "x"}}
		},
		"operation not object": func(in map[string]any) {
			in["steps"] = []any{map[string]any{"op": "concurrent", "operations": []any{nil}}}
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

// A known fault name at a point the operation does not have never fires.
func TestForeignFaultIgnored(t *testing.T) {
	in := fixtureInput(t, "sc-10-race-16")
	p := sjson.Clone(in["steps"].([]any)[0].(map[string]any)["promote"]).(map[string]any)
	p["op"] = "promote"
	in["steps"] = []any{p}
	plain := string(sjson.Encode(Run(sjson.Clone(in))))
	p2 := sjson.Clone(p).(map[string]any)
	p2["fault"] = "crash_after_dispatch"
	in["steps"] = []any{p2}
	if got := string(sjson.Encode(Run(in))); got != plain {
		t.Fatalf("got %s, want %s", got, plain)
	}
}

func TestOperationUnknown(t *testing.T) {
	in := fixtureInput(t, "sc-10-race-16")
	in["steps"] = []any{map[string]any{"op": "zz_unknown_operation"}, map[string]any{}}
	out := Run(in)
	want := `{"code":"OPERATION_UNKNOWN","result":"refused"}`
	steps := out["steps"].([]any)
	for i, s := range steps {
		if got := string(sjson.Encode(s)); got != want {
			t.Errorf("step %d: %s", i, got)
		}
	}
	if out["final"].(map[string]any)["refusals"] != int64(2) {
		t.Errorf("final %s", sjson.Encode(out["final"]))
	}
}
