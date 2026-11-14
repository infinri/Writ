package transition

import (
	"bufio"
	"os"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

// Runs the concurrent fixture cases repeatedly (use with -race); every run
// must produce an acceptable serialization.
func TestConcurrentFixtures(t *testing.T) {
	f, err := os.Open("../../../fixtures/sets/transition-scenarios.jsonl")
	if err != nil {
		t.Skip("fixtures not available:", err)
	}
	defer f.Close()
	want := map[string]bool{"sc-10-race-16": true, "sc-64-concurrent-invalidation-and-publication": true, "sc-39-crash-before-commit-then-retry": true}
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	seen := 0
	for sc.Scan() {
		v, perr := sjson.ParseUnlimited(sc.Text())
		if perr != nil {
			t.Fatal(perr)
		}
		c := v.(map[string]any)
		if !want[c["id"].(string)] {
			continue
		}
		seen++
		expect := c["expect"].(map[string]any)
		alts := []any{expect}
		if c["compare"] == "one_of" {
			alts = expect["one_of"].([]any)
		}
		for i := 0; i < 50; i++ {
			got := string(sjson.Encode(Run(c["input"])))
			ok := false
			for _, a := range alts {
				if string(sjson.Encode(a)) == got {
					ok = true
				}
			}
			if !ok {
				t.Fatalf("%s run %d: unexpected output %s", c["id"], i, got)
			}
		}
	}
	if seen != len(want) {
		t.Fatalf("found %d of %d cases", seen, len(want))
	}
}
