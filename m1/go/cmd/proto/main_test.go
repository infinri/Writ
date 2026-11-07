package main

import (
	"bufio"
	"bytes"
	"strings"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

// Protocol clarifications (M1-05): envelope rules.
func TestEnvelope(t *testing.T) {
	lines := []struct {
		line     string
		answered bool
		want     string // expected output line; "" when nothing is written
	}{
		{`{"id":"ok","input_text":"[1]"}`, true, `"canonical":"[1]"`},
		{`{"id":"in","input":[1]}`, true, `"canonical":"[1]"`},
		{`{"id":"x","input_text":"1","x":1}`, true, `{"id":"x","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"nz","input_text":"-00"}`, true, `{"id":"nz","output":{"error":"INVALID_JSON"}}`},
		{`{"id":"nan","input":NaN}`, true, `{"id":"nan","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"none"}`, true, `{"id":"none","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"both","input":1,"input_text":"1"}`, true, `{"id":"both","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"ts","input_text":1}`, true, `{"id":"ts","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"ls","input_text":"\ud800"}`, true, `{"id":"ls","output":{"error":"ENVELOPE_INVALID"}}`},
		{"{\"id\":\"u8\",\"input_text\":\"\xc3\x28\"}", true, `{"id":"u8","output":{"error":"ENVELOPE_INVALID"}}`},
		{`{"id":"cr","input_text":"1"}` + "\r", true, `"canonical":"1"`},
		{"   ", true, ""},
		{"\t\r", true, ""},
		{"", true, ""},
		{`{"id":7,"input_text":"1"}`, false, ""},
		{`{"id":"e","id":"f","input_text":"1"}`, false, ""},
		{`[1,2]`, false, ""},
		{`not json`, false, ""},
	}
	for _, c := range lines {
		var buf bytes.Buffer
		w := bufio.NewWriter(&buf)
		got := handleLine(canonical, c.line+"\n", w)
		w.Flush()
		out := strings.TrimSuffix(buf.String(), "\n")
		if got != c.answered {
			t.Errorf("%q: answered = %v, want %v", c.line, got, c.answered)
		}
		if c.want == "" && out != "" || c.want != "" && !strings.Contains(out, c.want) {
			t.Errorf("%q: output %q, want %q", c.line, out, c.want)
		}
	}
}

// No input may crash the process: a handler panic is answered INTERNAL_ERROR.
func TestRecoverBoundary(t *testing.T) {
	var buf bytes.Buffer
	w := bufio.NewWriter(&buf)
	boom := func(any, *sjson.Error) map[string]any { panic("boom") }
	if !handleLine(boom, `{"id":"p","input":1}`, w) {
		t.Fatal("line not answered")
	}
	w.Flush()
	if got := strings.TrimSpace(buf.String()); got != `{"id":"p","output":{"error":"INTERNAL_ERROR"}}` {
		t.Fatalf("got %s", got)
	}
}

// promote_concurrent with a negative count used to panic (M1-05).
func TestPromoteConcurrentNegativeCount(t *testing.T) {
	var buf bytes.Buffer
	w := bufio.NewWriter(&buf)
	line := `{"id":"n","input":{"initial":{},"steps":[{"op":"promote_concurrent","count":-1,"promote":{}}]}}`
	if !handleLine(transitionScenario, line, w) {
		t.Fatal("line not answered")
	}
	w.Flush()
	if got := strings.TrimSpace(buf.String()); got != `{"id":"n","output":{"input_error":"INPUT_MALFORMED"}}` {
		t.Fatalf("got %s", got)
	}
}
