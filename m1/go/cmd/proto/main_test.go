package main

import (
	"bufio"
	"bytes"
	"strings"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

func answerLine(h handler, line string) string {
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
