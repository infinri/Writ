package broker

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"writ.dev/m1/proto/internal/sjson"
)

// M1-06d: bench timing names the transactions LEDGER-SQLITE.md specifies,
// with the null rules of each ledger, and never changes the scenario output.

func txOutcomes(s map[string]any) map[string]string {
	out := map[string]string{}
	for name, v := range s["tx"].(map[string]any) {
		out[name] = v.(map[string]any)["outcome"].(string)
	}
	return out
}

func timedSteps(t *testing.T, id string, store *SQLite) []map[string]any {
	t.Helper()
	in := fixtureCase(t, id)
	want := string(sjson.Encode(RunWith(sjson.Clone(in), store)))
	tm := NewTiming(0)
	if got := string(sjson.Encode(RunTimed(sjson.Clone(in), store, tm))); got != want {
		t.Fatalf("%s: timed output %s, plain %s", id, got, want)
	}
	j := tm.JSON()
	if store == nil && j["open_ns"] != nil || store != nil && j["open_ns"] == nil {
		t.Errorf("%s: open_ns %v", id, j["open_ns"])
	}
	if j["final_ns"] == nil {
		t.Errorf("%s: final_ns null", id)
	}
	var steps []map[string]any
	for _, s := range j["steps"].([]any) {
		steps = append(steps, s.(map[string]any))
	}
	return steps
}

func TestTimingTransactions(t *testing.T) {
	all := map[string]string{"reserve": "commit", "dispatch": "commit", "adapter": "commit", "observe": "commit"}
	cases := []struct {
		id   string
		step int
		want map[string]string
	}{
		{"b-01-allow-path", 0, all},
		{"b-03-unsealed", 0, map[string]string{"audit": "commit"}},
		{"b-15-grant-single-use", 0, map[string]string{"approve": "commit"}},
		{"b-15-grant-single-use", 1, all},
		{"b-15-grant-single-use", 2, map[string]string{"reserve": "rollback", "audit": "commit"}},
		{"b-26-self-approval", 0, map[string]string{"audit": "commit"}},
		{"b-09-forged-permit", 1, map[string]string{"adapter": "rollback", "audit": "commit"}},
		{"b-30-idempotent-retry", 1, map[string]string{"reserve": "rollback"}},
		{"b-35-crash-after-reserve-resumes", 1, map[string]string{"reserve": "commit"}},
		{"b-35-crash-after-reserve-resumes", 2, all},
		{"b-40-reconcile-nothing", 0, map[string]string{"reconcile": "commit"}},
	}
	for _, ledger := range []string{"memory", "sqlite"} {
		var store *SQLite
		if ledger == "sqlite" {
			store = &SQLite{Dir: t.TempDir()}
		}
		for _, c := range cases {
			s := timedSteps(t, c.id, store)[c.step]
			if got := txOutcomes(s); !reflect.DeepEqual(got, c.want) {
				t.Errorf("%s %s step %d: tx %v, want %v", ledger, c.id, c.step, got, c.want)
			}
			for name, v := range s["tx"].(map[string]any) {
				tx := v.(map[string]any)
				if (tx["begin_ns"] == nil) != (ledger == "memory") || (tx["end_ns"] == nil) != (ledger == "memory") {
					t.Errorf("%s %s step %d %s: begin %v end %v", ledger, c.id, c.step, name, tx["begin_ns"], tx["end_ns"])
				}
			}
			execute := s["op"] == "execute"
			_, reserved := c.want["reserve"]
			if (s["key_lock_wait_ns"] != nil) != (ledger == "sqlite" && reserved) {
				t.Errorf("%s %s step %d: key_lock_wait_ns %v", ledger, c.id, c.step, s["key_lock_wait_ns"])
			}
			if (s["validate_ns"] != nil) != (execute || s["op"] == "approve") {
				t.Errorf("%s %s step %d: validate_ns %v", ledger, c.id, c.step, s["validate_ns"])
			}
		}
		// An unsealed request never reaches the decision.
		if s := timedSteps(t, "b-03-unsealed", store)[0]; s["decide_ns"] != nil {
			t.Errorf("%s: unsealed decide_ns %v", ledger, s["decide_ns"])
		}
		// A concurrent step has one entry per operation, in input order.
		s := timedSteps(t, "b-34-concurrent-duplicates", store)[0]
		ops := fixtureCase(t, "b-34-concurrent-duplicates")["steps"].([]any)[0].(map[string]any)["operations"].([]any)
		if s["op"] != "concurrent" || len(s["tx"].(map[string]any)) != 0 || len(s["operations"].([]any)) != len(ops) {
			t.Errorf("%s: concurrent step %v", ledger, s)
		}
	}
}

// Timing of an input that never runs: no steps, final_ns null.
func TestTimingMalformed(t *testing.T) {
	tm := NewTiming(0)
	RunTimed(map[string]any{}, nil, tm)
	j := tm.JSON()
	if len(j["steps"].([]any)) != 0 || j["final_ns"] != nil || j["open_ns"] != nil {
		t.Fatalf("got %v", j)
	}
}

// run_ns ends only after the scenario's ledger is closed, on the normal path
// and when initialization fails (LEDGER-SQLITE.md "Per-operation timing").
func TestRunCoversLedgerClose(t *testing.T) {
	var closedAt time.Time
	onClose = func() { closedAt = time.Now() }
	defer func() { onClose = nil }()
	dir := t.TempDir()
	wrong := filepath.Join(t.TempDir(), "wrong.db")
	execOn(t, wrong, `CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); INSERT INTO meta VALUES ('schema_version', '2')`)
	for _, c := range []struct {
		name  string
		store *SQLite
		want  string
	}{
		{"normal", &SQLite{Dir: dir}, `"steps"`},
		{"initialization fails", &SQLite{DB: wrong}, `{"error":"LEDGER_UNAVAILABLE"}`},
	} {
		closedAt = time.Time{}
		tm := NewTiming(0)
		var out []byte
		stderrOf(t, func() {
			out = sjson.Encode(RunTimed(sjson.Clone(fixtureCase(t, "b-34-concurrent-duplicates")), c.store, tm))
		})
		if !strings.Contains(string(out), c.want) {
			t.Fatalf("%s: output %s", c.name, out)
		}
		if closedAt.IsZero() {
			t.Fatalf("%s: no ledger connection was closed", c.name)
		}
		if end := tm.start.Add(tm.run); closedAt.After(end) {
			t.Fatalf("%s: ledger closed %v after run_ns ended", c.name, closedAt.Sub(end))
		}
	}
	// The normal run's WAL was checkpointed and removed within run_ns.
	if wal, _ := filepath.Glob(filepath.Join(dir, "*-wal")); len(wal) != 0 {
		t.Fatalf("WAL left behind: %v", wal)
	}
}
