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
	"check-acceptance":    checkAcceptance,
	"external-refusal":    externalRefusal,
	"transition-scenario": transitionScenario,
	"broker-scenario":     brokerScenario,
}

func main() {
	if len(os.Args) != 2 || commands[os.Args[1]] == nil {
		fmt.Fprintln(os.Stderr, "usage: proto canonical|decide|decide-batch|check-acceptance|external-refusal|transition-scenario|broker-scenario")
		os.Exit(2)
	}
	h := commands[os.Args[1]]
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	failed := false
	for {
		line, err := in.ReadString('\n')
		if len(line) > 0 {
			if !handleLine(h, line, out) {
				failed = true
			}
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
	out.Flush()
	if failed {
		os.Exit(1)
	}
}

// handleLine answers one envelope; it returns false if the line could not be
// answered (no id).
func handleLine(h handler, line string, out *bufio.Writer) bool {
	for len(line) > 0 && (line[len(line)-1] == '\n' || line[len(line)-1] == '\r') {
		line = line[:len(line)-1]
	}
	if line == "" {
		return true
	}
	env, perr := sjson.ParseUnlimited(line)
	m, ok := sjson.Obj(env)
	id, okID := sjson.Str(m, "id")
	if perr != nil || !ok || !okID {
		fmt.Fprintln(os.Stderr, "unreadable envelope line")
		return false
	}
	var input any
	var jerr *sjson.Error
	if text, isText := sjson.Str(m, "input_text"); isText {
		input, jerr = sjson.Parse(text)
	} else {
		input = m["input"]
		jerr = sjson.CheckDepth(input)
	}
	output := h(input, jerr)
	out.Write(sjson.Encode(map[string]any{"id": id, "output": output}))
	out.WriteByte('\n')
	return true
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
		d := &kernel.Decision{RequestID: "unknown", Status: "invalid", ReasonCode: "REQUEST_JSON_INVALID",
			MatchedRuleIDs: []string{}, PolicySha: kernel.NoPolicySha}
		return d.JSON()
	}
	env, _ := sjson.Obj(input)
	rev, okRev := sjson.Int(env, "state_revision")
	nowS, _ := sjson.Str(env, "now")
	now, okNow := kernel.ParseTime(nowS)
	if !okRev || !okNow {
		d := &kernel.Decision{RequestID: "unknown", Status: "invalid", ReasonCode: "REQUEST_SCHEMA_INVALID",
			MatchedRuleIDs: []string{}, PolicySha: kernel.NoPolicySha, StateRevision: rev, DecidedAt: now}
		return d.JSON()
	}
	return kernel.Decide(env["request"], kernel.LoadPolicy(env["bundle"]), rev, now).JSON()
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
