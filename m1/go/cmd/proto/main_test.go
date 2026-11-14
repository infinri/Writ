package main

import (
	"bufio"
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

func answerLine(h handler, line string) string {
	return answerLineWith(plain(h), line)
}

func answerLineWith(h lineHandler, line string) string {
	var buf bytes.Buffer
	w := bufio.NewWriter(&buf)
	handleLine(h, line, w)
	w.Flush()
	return buf.String()
}

// D-021 envelope rules: closed structure; the id is preserved only from a
// strictly parsed object with exactly one string id, otherwise null; every
// non-blank line is answered exactly once.
func TestEnvelope(t *testing.T) {
	const inv = `"output":{"error":"ENVELOPE_INVALID"}}`
	lines := []struct{ line, want string }{
		{`{"id":"ok","input_text":"[1]"}`, `{"id":"ok","output":{"canonical":"[1]"`},
		{`{"id":"in","input":[1]}`, `{"id":"in","output":{"canonical":"[1]"`},
		{`{"input":[1],"id":"ord"}`, `{"id":"ord","output":{"canonical":"[1]"`},
		{`{"id":"x","input_text":"1","x":1}`, `{"id":"x",` + inv},
		{`{"id":"x","input":1,"meta":null}`, `{"id":"x",` + inv},
		{`{"id":"nz","input_text":"-00"}`, `{"id":"nz","output":{"error":"INVALID_JSON"}}`},
		{`{"id":"none"}`, `{"id":"none",` + inv},
		{`{"id":"both","input":1,"input_text":"1"}`, `{"id":"both",` + inv},
		{`{"id":"ts","input_text":1}`, `{"id":"ts",` + inv},
		{`{"id":"","input_text":"1"}`, `{"id":"","output":{"canonical":"1"`},
		{`{"id":"cr","input_text":"1"}` + "\r", `{"id":"cr","output":{"canonical":"1"`},
		// Not strictly valid JSON: no id is recovered.
		{`{"id":"nan","input":NaN}`, `{"id":null,` + inv},
		{`{"id":"ls","input_text":"\ud800"}`, `{"id":null,` + inv},
		{"{\"id\":\"u8\",\"input_text\":\"\xc3\x28\"}", `{"id":null,` + inv},
		{`{"id":"e","id":"f","input_text":"1"}`, `{"id":null,` + inv},
		{`{"id":"e","\u0069d":"e","input_text":"1"}`, `{"id":null,` + inv},
		{`{"id":"t","input_text":"1"} x`, `{"id":null,` + inv},
		{`{"id":"t","input_text":"1"`, `{"id":null,` + inv},
		{`{"id":"t","input_text":"1",}`, `{"id":null,` + inv},
		{`[1,2]`, `{"id":null,` + inv},
		{`"id"`, `{"id":null,` + inv},
		{`not json`, `{"id":null,` + inv},
		// Strictly parsed, but no single string id.
		{`{"id":7,"input_text":"1"}`, `{"id":null,` + inv},
		{`{"id":null,"input_text":"1"}`, `{"id":null,` + inv},
		{`{"id":["a"],"input_text":"1"}`, `{"id":null,` + inv},
		{`{"input_text":"1"}`, `{"id":null,` + inv},
		{`{}`, `{"id":null,` + inv},
	}
	for _, c := range lines {
		out := answerLine(canonical, c.line+"\n")
		if strings.Count(out, "\n") != 1 || !strings.HasPrefix(out, c.want) {
			t.Errorf("%q: output %q, want prefix %q", c.line, out, c.want)
		}
	}
	for _, blank := range []string{"", "   ", "\t\r"} {
		if out := answerLine(canonical, blank+"\n"); out != "" {
			t.Errorf("blank %q answered %q", blank, out)
		}
	}
}

// D-021 (1): a decide strict-parse failure is exactly three members.
func TestDecideParseFailure(t *testing.T) {
	const want = `{"id":"d","output":{"matched_rule_ids":[],"reason_code":"REQUEST_JSON_INVALID","status":"invalid"}}` + "\n"
	for _, text := range []string{`{`, `{"a":1,"a":2}`, `{"x":NaN}`, `-00`, `"\ud800"`} {
		line := string(sjson.Encode(map[string]any{"id": "d", "input_text": text}))
		for _, h := range []handler{commands["decide"], commands["decide-batch"]} {
			if out := answerLine(h, line); out != want {
				t.Errorf("%q: got %s", text, out)
			}
		}
	}
}

// D-022: malformed `now`, `state_revision` or a missing `request` is refused
// before any decision, with exactly three members and no placeholder metadata.
func TestDecideHarnessFieldsRefuse(t *testing.T) {
	const refusal = `{"id":"d","output":{"matched_rule_ids":[],"reason_code":"REQUEST_SCHEMA_INVALID","status":"invalid"}}`
	for _, input := range []string{
		`{"bundle":null,"state_revision":1,"request":{}}`,
		`{"bundle":null,"state_revision":1,"now":"2026-02-30T00:00:00Z","request":{}}`,
		`{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00+00:00","request":{}}`,
		`{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00.1Z","request":{}}`,
		`{"bundle":null,"state_revision":-1,"now":"2026-01-01T00:00:00Z","request":{}}`,
		`{"bundle":null,"state_revision":"1","now":"2026-01-01T00:00:00Z","request":{}}`,
		`{"bundle":null,"state_revision":1,"now":"2026-01-01T00:00:00Z"}`,
		`[]`,
	} {
		if got := strings.TrimSpace(answerLine(commands["decide"], `{"id":"d","input":`+input+`}`)); got != refusal {
			t.Errorf("%s: got %s", input, got)
		}
	}
	ok := answerLine(commands["decide"], `{"id":"d","input":{"bundle":null,"state_revision":9007199254740991,"now":"2024-02-29T23:59:59Z","request":{}}}`)
	if !strings.Contains(ok, `"decided_at":"2024-02-29T23:59:59Z"`) {
		t.Errorf("valid boundary input: %s", ok)
	}
}

// bench-decide wraps exactly the decide output with a non-negative kernel_ns.
func TestBenchDecideWrapsDecide(t *testing.T) {
	for _, line := range []string{
		`{"id":"d","input_text":"{"}`,
		`{"id":"d","input":{"bundle":null,"state_revision":0,"now":"2026-01-01T00:00:00Z","request":{}}}`,
	} {
		plain := mustDecode(t, answerLine(commands["decide"], line))
		timed, _ := sjson.Obj(mustDecode(t, answerLine(commands["bench-decide"], line))["output"])
		if got, want := string(sjson.Encode(timed["decision"])), string(sjson.Encode(plain["output"])); got != want {
			t.Errorf("%s: decision %s, want %s", line, got, want)
		}
		if ns, ok := sjson.Int(timed, "kernel_ns"); !ok || ns < 0 || len(timed) != 2 {
			t.Errorf("%s: kernel_ns %v, members %d", line, timed["kernel_ns"], len(timed))
		}
	}
	if got := strings.TrimSpace(answerLine(commands["bench-decide"], `{"id":"d"}`)); got != `{"id":"d","output":{"error":"ENVELOPE_INVALID"}}` {
		t.Errorf("got %s", got)
	}
}

// bench-broker-scenario wraps exactly the broker-scenario output and takes
// the same ledger options.
func TestBenchBrokerScenario(t *testing.T) {
	dir := t.TempDir()
	for _, opts := range [][]string{nil, {"--ledger", "sqlite", "--ledger-dir", dir}} {
		plainH, ok1 := parseArgs(append([]string{"broker-scenario"}, opts...))
		benchH, ok2 := parseArgs(append([]string{"bench-broker-scenario"}, opts...))
		if !ok1 || !ok2 {
			t.Fatalf("%v rejected", opts)
		}
		for _, line := range []string{
			`{"id":"j","input_text":"{"}`,
			`{"id":"m","input":{}}`,
			string(sjson.Encode(map[string]any{"id": "f", "input": fixtureInput(t, "b-15-grant-single-use")})),
		} {
			plain := mustDecode(t, answerLineWith(plainH, line))
			timed, _ := sjson.Obj(mustDecode(t, answerLineWith(benchH, line))["output"])
			if got, want := string(sjson.Encode(timed["scenario"])), string(sjson.Encode(plain["output"])); got != want || len(timed) != 2 {
				t.Errorf("%v %s: scenario %s, want %s", opts, line, got, want)
			}
			tm, _ := sjson.Obj(timed["timing"])
			if _, ok := sjson.Int(tm, "parse_ns"); !ok || len(tm) != 5 {
				t.Errorf("%v %s: timing %v", opts, line, tm)
			}
		}
	}
	if _, ok := parseArgs([]string{"bench-broker-scenario", "--ledger", "sqlite"}); ok {
		t.Error("incomplete ledger options accepted")
	}
}

func mustDecode(t *testing.T, s string) map[string]any {
	t.Helper()
	v, err := sjson.Parse(strings.TrimSpace(s))
	if err != nil {
		t.Fatal(err)
	}
	m, _ := sjson.Obj(v)
	return m
}

// No input may crash the process: a handler panic is answered INTERNAL_ERROR.
func TestRecoverBoundary(t *testing.T) {
	boom := func(any, *sjson.Error) map[string]any { panic("boom") }
	if got := strings.TrimSpace(answerLine(boom, `{"id":"p","input":1}`)); got != `{"id":"p","output":{"error":"INTERNAL_ERROR"}}` {
		t.Fatalf("got %s", got)
	}
}

// promote_concurrent with a negative count used to panic (M1-05).
func TestPromoteConcurrentNegativeCount(t *testing.T) {
	line := `{"id":"n","input":{"initial":{},"steps":[{"op":"promote_concurrent","count":-1,"promote":{}}]}}`
	if got := strings.TrimSpace(answerLine(transitionScenario, line)); got != `{"id":"n","output":{"input_error":"INPUT_MALFORMED"}}` {
		t.Fatalf("got %s", got)
	}
}

// TestMain lets a test run this binary's main in a child process.
func TestMain(m *testing.M) {
	if args := os.Getenv("PROTO_TEST_ARGS"); args != "" {
		os.Args = append([]string{"proto"}, strings.Split(args, "\x1f")...)
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func runProto(t *testing.T, stdin string, args ...string) string {
	t.Helper()
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), "PROTO_TEST_ARGS="+strings.Join(args, "\x1f"))
	cmd.Stdin = strings.NewReader(stdin)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("%v: %v: %s", args, err, stderr.String())
	}
	return string(out)
}

// Only broker-scenario takes ledger options, in exactly one form.
func TestLedgerOptions(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "f")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"broker-scenario"},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", dir},
		{"broker-scenario", "--ledger-dir", dir, "--ledger", "sqlite"},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-db", filepath.Join(dir, "x.db")},
		{"broker-scenario", "--concurrency-profile", "primary"},
		{"bench-broker-scenario", "--concurrency-profile", "cold"},
		{"broker-scenario", "--concurrency-profile", "cold", "--ledger", "sqlite", "--ledger-dir", dir},
		{"bench-broker-scenario", "--ledger", "sqlite", "--concurrency-profile", "primary", "--ledger-dir", dir},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-db", filepath.Join(dir, "x.db"), "--concurrency-profile", "cold"},
	} {
		if _, ok := parseArgs(args); !ok {
			t.Errorf("%v rejected", args)
		}
	}
	for _, args := range [][]string{
		{},
		{"nope"},
		{"decide", "--ledger", "sqlite", "--ledger-dir", dir},
		{"transition-scenario", "--ledger", "sqlite", "--ledger-db", "x.db"},
		{"sqlite-info", "--ledger", "sqlite"},
		{"broker-scenario", "--ledger", "sqlite"},
		{"broker-scenario", "--ledger", "memory", "--ledger-dir", dir},
		{"broker-scenario", "--ledger-dir", dir},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", dir, "--ledger-db", "x.db"},
		{"broker-scenario", "--ledger", "sqlite", "--ledger", "sqlite", "--ledger-dir", dir},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir"},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", ""},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", file},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", filepath.Join(dir, "missing")},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-db", "x.db", "extra"},
		{"broker-scenario", "--concurrency-profile", "warm"},
		{"broker-scenario", "--concurrency-profile", "Primary"},
		{"broker-scenario", "--concurrency-profile", ""},
		{"broker-scenario", "--concurrency-profile"},
		{"bench-broker-scenario", "--concurrency-profile", "cold", "--concurrency-profile", "cold"},
		{"broker-scenario", "--concurrency-profile", "cold", "--ledger", "sqlite"},
		{"broker-scenario", "--ledger", "sqlite", "--ledger-dir", dir, "--concurrency-profile"},
		{"decide", "--concurrency-profile", "cold"},
		{"transition-scenario", "--concurrency-profile", "primary"},
	} {
		if _, ok := parseArgs(args); ok {
			t.Errorf("%v accepted", args)
		}
	}
}

func fixtureInput(t *testing.T, id string) map[string]any {
	t.Helper()
	data, err := os.ReadFile("../../../fixtures/sets/broker-scenarios.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		v, perr := sjson.ParseUnlimited(line)
		if perr != nil {
			t.Fatal(perr)
		}
		if c := v.(map[string]any); c["id"] == id {
			return c["input"].(map[string]any)
		}
	}
	t.Fatalf("fixture %s not found", id)
	return nil
}

// LEDGER-SQLITE.md required test 3: a second process opening the same
// --ledger-db file continues from the ledger the first one left.
func TestSecondProcessContinuesLedger(t *testing.T) {
	race, dup := fixtureInput(t, "b-27-grant-race-16"), fixtureInput(t, "b-34-concurrent-duplicates")
	initial := race["initial"].(map[string]any)
	approve := race["steps"].([]any)[0]
	approval := race["steps"].([]any)[1].(map[string]any)["operations"].([]any)[0].(map[string]any)
	allow := dup["steps"].([]any)[0].(map[string]any)["operations"].([]any)[0].(map[string]any)
	ingress, _ := hex.DecodeString(initial["ingress_keys"].(map[string]any)["writ-host-adapter:fixture"].(string))
	execute := func(op map[string]any, key, fault string) map[string]any {
		o := sjson.Clone(op).(map[string]any)
		req := o["request"].(map[string]any)
		req["idempotency_key"] = key
		h := hmac.New(sha256.New, ingress)
		h.Write(sjson.MustCanonical(req))
		o["seal"].(map[string]any)["mac"] = hex.EncodeToString(h.Sum(nil))
		if fault != "" {
			o["fault"] = fault
		}
		return o
	}
	line := func(steps ...any) string {
		return string(sjson.Encode(map[string]any{"id": "s", "input": map[string]any{"initial": initial, "steps": steps}})) + "\n"
	}
	db := filepath.Join(t.TempDir(), "ledger.db")
	args := []string{"broker-scenario", "--ledger", "sqlite", "--ledger-db", db}

	first := runProto(t, line(
		approve,
		execute(approval, "p-a", "crash_after_reserve"),
		execute(allow, "p-b", "crash_after_dispatch"),
		execute(allow, "p-c", ""),
		map[string]any{"op": "apply_without_permit"},
	), args...)
	want := `{"id":"s","output":{"final":{"consumed_permits":2,"effects_applied":2,"grant_uses":{"g1":1},` +
		`"receipts":{"dispatched":1,"observed_success":1,"reserved":1},"refusals":1},"steps":[{"result":"grant_issued"},` +
		`{"result":"crashed"},{"result":"crashed"},{"result":"effect_completed"},{"code":"PERMIT_MISSING","result":"refused"}]}}` + "\n"
	if first != want {
		t.Fatalf("first process:\n%s\nwant\n%s", first, want)
	}

	second := runProto(t, line(
		execute(approval, "p-a", ""),
		execute(allow, "p-b", ""),
		execute(allow, "p-c", ""),
		approve,
		execute(allow, "p-d", ""),
	), args...)
	// p-a resumes without a second grant use; p-b stays uncertain and its
	// effect is not applied again; p-c replays; g1 persists; permit ids
	// continue from the persisted next_permit (a reset would replay permit-1).
	want = `{"id":"s","output":{"final":{"consumed_permits":4,"effects_applied":4,"grant_uses":{"g1":1},` +
		`"receipts":{"dispatched":1,"observed_success":3},"refusals":3},"steps":[{"result":"effect_completed"},` +
		`{"code":"RECONCILIATION_REQUIRED","result":"refused"},{"replayed":true,"result":"effect_completed"},` +
		`{"code":"GRANT_REF_IN_USE","result":"refused"},{"result":"effect_completed"}]}}` + "\n"
	if second != want {
		t.Fatalf("second process:\n%s\nwant\n%s", second, want)
	}
}

func TestSQLiteInfoCommand(t *testing.T) {
	out := runProto(t, "", "sqlite-info")
	for _, want := range []string{`"sqlite_version":"3.54.1"`, `"journal_mode":"wal"`, `"synchronous":2`, `"busy_timeout":5000`,
		`"foreign_keys":1`, `"trusted_schema":0`, `"driver":"github.com/mattn/go-sqlite3 v1.14.52"`} {
		if !strings.Contains(out, want) {
			t.Errorf("sqlite-info lacks %s: %s", want, out)
		}
	}
}

// D-027, D-028: --concurrency-profile selects when worker connections are
// set up; the scenario output is the same in both profiles, the group
// carries ready_ns, release_ns and start_skew_ns, and every worker setup_ns
// (null for the in-memory ledger), ready_at_ns, start_ns and
// release_delay_ns.
func TestConcurrencyProfileOption(t *testing.T) {
	line := string(sjson.Encode(map[string]any{"id": "c", "input": fixtureInput(t, "b-34-concurrent-duplicates")}))
	want := mustDecode(t, answerLine(brokerScenario, line))["output"]
	for _, ledger := range [][]string{nil, {"--ledger", "sqlite", "--ledger-dir", t.TempDir()}} {
		for _, prof := range []string{"primary", "cold"} {
			args := append([]string{"--concurrency-profile", prof}, ledger...)
			plainH, ok1 := parseArgs(append([]string{"broker-scenario"}, args...))
			benchH, ok2 := parseArgs(append([]string{"bench-broker-scenario"}, args...))
			if !ok1 || !ok2 {
				t.Fatalf("%v rejected", args)
			}
			if got := mustDecode(t, answerLineWith(plainH, line))["output"]; string(sjson.Encode(got)) != string(sjson.Encode(want)) {
				t.Errorf("%v: output %v", args, got)
			}
			timed, _ := sjson.Obj(mustDecode(t, answerLineWith(benchH, line))["output"])
			if string(sjson.Encode(timed["scenario"])) != string(sjson.Encode(want)) {
				t.Errorf("%v: scenario %v", args, timed["scenario"])
			}
			tm, _ := sjson.Obj(timed["timing"])
			group := tm["steps"].([]any)[0].(map[string]any)
			for _, k := range []string{"ready_ns", "release_ns", "start_skew_ns"} {
				if _, ok := sjson.Int(group, k); !ok {
					t.Errorf("%v: group %s %v", args, k, group)
				}
			}
			for _, o := range group["operations"].([]any) {
				w := o.(map[string]any)
				_, hasStart := sjson.Int(w, "start_ns")
				for _, k := range []string{"ready_at_ns", "release_delay_ns"} {
					if _, ok := sjson.Int(w, k); !ok {
						t.Errorf("%v: worker %s %v", args, k, w)
					}
				}
				_, setupNum := sjson.Int(w, "setup_ns")
				if setup, ok := w["setup_ns"]; !ok || !hasStart || (ledger == nil) != (setup == nil) || (ledger != nil) != setupNum {
					t.Errorf("%v: worker %v", args, w)
				}
			}
		}
	}
}
