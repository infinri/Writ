// Command proto is the Go M1 prototype (M1-04): `proto <command>` reads JSON
// Lines {"id", "input"} or {"id", "input_text"} on stdin and writes one
// {"id", "output"} per line (docs/implementation/M1-PARITY-SUITE.md).
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"time"

	"writ.dev/m1/proto/internal/acceptance"
	"writ.dev/m1/proto/internal/broker"
	"writ.dev/m1/proto/internal/kernel"
	"writ.dev/m1/proto/internal/refusal"
	"writ.dev/m1/proto/internal/sjson"
	"writ.dev/m1/proto/internal/transition"
)

type handler func(input any, jsonErr *sjson.Error) map[string]any

var commands = map[string]handler{
	"canonical":           canonical,
	"decide":              decide,
	"decide-batch":        decide,
	"bench-decide":        benchDecide,
	"check-acceptance":    checkAcceptance,
	"external-refusal":    externalRefusal,
	"transition-scenario": transitionScenario,
	"broker-scenario":     brokerScenario,
}

func main() {
	if len(os.Args) != 2 || commands[os.Args[1]] == nil {
		fmt.Fprintln(os.Stderr, "usage: proto canonical|decide|decide-batch|bench-decide|check-acceptance|external-refusal|transition-scenario|broker-scenario")
		os.Exit(2)
	}
	h := commands[os.Args[1]]
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	for {
		line, err := in.ReadString('\n')
		if len(line) > 0 {
			handleLine(h, line, out)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "read:", err)
			out.Flush()
			os.Exit(1)
		}
	}
}

// isBlank reports whether a line holds only JSON whitespace.
func isBlank(line string) bool {
	for i := 0; i < len(line); i++ {
		switch line[i] {
		case ' ', '\t', '\r', '\n':
		default:
			return false
		}
	}
	return true
}

// handleLine answers one envelope line (D-021): every non-blank line gets
// exactly one answer. Blank lines are ignored.
func handleLine(h handler, line string, out *bufio.Writer) {
	for len(line) > 0 && (line[len(line)-1] == '\n' || line[len(line)-1] == '\r') {
		line = line[:len(line)-1]
	}
	if isBlank(line) {
		return
	}
	id, output := answer(h, line)
	out.Write(sjson.Encode(map[string]any{"id": id, "output": output}))
	out.WriteByte('\n')
}

// envelopeInvalid is the answer to a line that is not a valid envelope (D-021).
var envelopeInvalid = map[string]any{"error": "ENVELOPE_INVALID"}

// answer parses and validates one envelope line strictly and runs the
// handler. A valid envelope is closed: exactly `id` (string) and one of
// `input` or `input_text` (string). The returned id is the envelope's string
// id only when the line parsed strictly as a JSON object with exactly one
// string `id` member (the strict parser rejects duplicate members); otherwise
// it is nil, never an id recovered from a line that is not valid JSON.
func answer(h handler, line string) (id any, output map[string]any) {
	env, perr := sjson.ParseUnlimited(line)
	m, isObj := sjson.Obj(env)
	if perr != nil || !isObj {
		return nil, envelopeInvalid
	}
	sid, ok := m["id"].(string)
	if !ok {
		return nil, envelopeInvalid
	}
	_, hasInput := m["input"]
	textV, hasText := m["input_text"]
	text, textIsStr := textV.(string)
	if hasInput == hasText || (hasText && !textIsStr) || len(m) != 2 {
		return sid, envelopeInvalid
	}
	var input any
	var jerr *sjson.Error
	if hasText {
		input, jerr = sjson.Parse(text)
	} else {
		input = m["input"]
		jerr = sjson.CheckDepth(input)
	}
	return sid, run(h, input, jerr)
}

// run calls the handler behind a recover boundary: no input may crash the
// process. A recovered panic is a prototype defect, answered INTERNAL_ERROR
// and reported on stderr.
func run(h handler, input any, jerr *sjson.Error) (output map[string]any) {
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintln(os.Stderr, "internal error:", r)
			output = map[string]any{"error": "INTERNAL_ERROR"}
		}
	}()
	return h(input, jerr)
}

func canonical(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		return map[string]any{"error": jerr.Code}
	}
	b, err := sjson.Canonical(input)
	if err != nil {
		if e, ok := err.(*sjson.Error); ok {
			return map[string]any{"error": e.Code}
		}
		return map[string]any{"error": sjson.ErrInvalidJSON}
	}
	sum := sha256.Sum256(b)
	return map[string]any{"canonical": string(b), "digest": sjson.DigestPrefix + hex.EncodeToString(sum[:])}
}

func decide(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		// D-021 (1): a protocol refusal with exactly these three members.
		return map[string]any{"status": "invalid", "reason_code": "REQUEST_JSON_INVALID", "matched_rule_ids": []any{}}
	}
	// D-022: the harness-supplied clock and revision are validated before any
	// decision. A failure is a three-field refusal, never a Decision with
	// placeholder metadata: `now` must be the strict YYYY-MM-DDTHH:MM:SSZ form
	// with a valid calendar time, `state_revision` an integer from 0 to 2^53-1
	// (the strict parser bounds the magnitude), and `request` present.
	env, isObj := sjson.Obj(input)
	rev, okRev := sjson.Int(env, "state_revision")
	nowS, okS := sjson.Str(env, "now")
	now, okNow := acceptance.ParseTimestamp(nowS)
	_, hasReq := env["request"]
	if !isObj || !okRev || rev < 0 || !okS || !okNow || !hasReq {
		return map[string]any{"status": "invalid", "reason_code": "REQUEST_SCHEMA_INVALID", "matched_rule_ids": []any{}}
	}
	return kernel.Decide(env["request"], kernel.LoadPolicy(env["bundle"]), rev, now).JSON()
}

// benchDecide (M1-06a) answers {"decision": <the decide output>, "kernel_ns": n},
// where n is the monotonic time spent producing the decision from the already
// parsed input, excluding envelope and input parsing, output encoding and I/O.
func benchDecide(input any, jerr *sjson.Error) map[string]any {
	started := time.Now()
	d := decide(input, jerr)
	return map[string]any{"decision": d, "kernel_ns": time.Since(started).Nanoseconds()}
}

func checkAcceptance(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		return map[string]any{"refusal": "INPUT_JSON_INVALID", "json_error": jerr.Code}
	}
	env, _ := sjson.Obj(input)
	return acceptance.Result(acceptance.Check(env["record"], env["current"], env["requirements"]))
}

func externalRefusal(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		return map[string]any{"external": refusal.External("INPUT_JSON_INVALID")}
	}
	env, _ := sjson.Obj(input)
	return map[string]any{"external": refusal.External(sjson.StrOr(env, "internal_code"))}
}

func transitionScenario(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		return map[string]any{"input_error": "INPUT_JSON_INVALID", "json_error": jerr.Code}
	}
	return transition.Run(input)
}

func brokerScenario(input any, jerr *sjson.Error) map[string]any {
	if jerr != nil {
		return map[string]any{"input_error": "INPUT_JSON_INVALID", "json_error": jerr.Code}
	}
	return broker.Run(input)
}
