package broker

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// D-027 (LEDGER-SQLITE.md "Concurrency profiles"), Go unit tests: in primary
// every worker connection is set up before release and no transaction begins
// before release; in cold each connection is set up after release; a failed
// setup refuses only that worker.

type traceLog struct {
	mu     sync.Mutex
	events []string
}

func (l *traceLog) add(event string, worker int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.events = append(l.events, fmt.Sprintf("%s %d", event, worker))
}

// split returns the events before and after "release -1".
func (l *traceLog) split(t *testing.T) (before, after []string) {
	t.Helper()
	for i, e := range l.events {
		if e == "release -1" {
			return l.events[:i], l.events[i+1:]
		}
	}
	t.Fatalf("no release in %v", l.events)
	return nil, nil
}

func nEvents(events []string, kind string) int {
	n := 0
	for _, e := range events {
		if strings.HasPrefix(e, kind+" ") {
			n++
		}
	}
	return n
}

func groupOp(t *testing.T, n int) map[string]any {
	ops := make([]any, n)
	for i := range ops {
		key := fmt.Sprintf("k%d", i%3) // shared keys: replays and key-lock waits too
		ops[i] = rtExecute(t, key, "")
	}
	return map[string]any{"op": "concurrent", "operations": ops}
}

// runGroup runs one timed concurrent group on b and returns its timing.
func runGroup(t *testing.T, b *Broker, op map[string]any) (map[string]any, map[string]any) {
	t.Helper()
	tm := newOpTiming(op)
	started := time.Now()
	tm.setDispatch(started)
	res := b.applyOn(b.led, tm, op, 0)
	tm.setTotal(time.Since(started))
	return res, tm.json()
}

// checkStartRule is the D-027 integrity rule for one group.
func checkStartRule(t *testing.T, g map[string]any, sqlite, cold bool) {
	t.Helper()
	ready, total := g["ready_ns"].(int64), g["total_ns"].(int64)
	if ready < 0 || ready > total {
		t.Fatalf("ready_ns %d, total_ns %d", ready, total)
	}
	for _, o := range g["operations"].([]any) {
		w := o.(map[string]any)
		start, wt := w["start_ns"].(int64), w["total_ns"].(int64)
		if start < ready || start+wt > total {
			t.Errorf("worker start %d total %d, group ready %d total %d", start, wt, ready, total)
		}
		if !sqlite {
			if w["setup_ns"] != nil {
				t.Errorf("memory setup_ns %v", w["setup_ns"])
			}
			continue
		}
		setup := w["setup_ns"].(int64)
		parts := int64(0)
		for _, v := range w["tx"].(map[string]any) {
			tx := v.(map[string]any)
			parts += tx["begin_ns"].(int64) + tx["body_ns"].(int64) + tx["end_ns"].(int64)
		}
		for _, k := range []string{"validate_ns", "decide_ns", "key_lock_wait_ns"} {
			if v, ok := w[k].(int64); ok {
				parts += v
			}
		}
		if cold && parts+setup > wt || !cold && (setup > ready || parts > wt) {
			t.Errorf("cold %v: setup %d parts %d total %d ready %d", cold, setup, parts, wt, ready)
		}
	}
}

func TestPrimarySetsUpEveryWorkerBeforeAnyTransaction(t *testing.T) {
	const n = 16
	for run := 0; run < 5; run++ {
		b, _ := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
		var log traceLog
		b.trace = log.add
		res, g := runGroup(t, b, groupOp(t, n))
		rtWant(t, res, `{"outcomes":{"effect_completed":3,"replayed":13},"result":"concurrent"}`)
		before, after := log.split(t)
		if nEvents(before, "setup") != n || nEvents(before, "ready") != n || nEvents(before, "begin") != 0 {
			t.Fatalf("before release: %v", before)
		}
		if nEvents(after, "setup") != 0 || nEvents(after, "ready") != 0 || nEvents(after, "begin") < n {
			t.Fatalf("after release: %v", after)
		}
		checkStartRule(t, g, true, false)
	}
}

func TestColdSetsUpEachWorkerAfterRelease(t *testing.T) {
	const n = 16
	for run := 0; run < 5; run++ {
		b, _ := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
		b.profile = Cold
		var log traceLog
		b.trace = log.add
		res, g := runGroup(t, b, groupOp(t, n))
		rtWant(t, res, `{"outcomes":{"effect_completed":3,"replayed":13},"result":"concurrent"}`)
		before, after := log.split(t)
		if len(before) != 0 {
			t.Fatalf("before release: %v", before)
		}
		if nEvents(after, "setup") != n || nEvents(after, "ready") != n {
			t.Fatalf("after release: %v", after)
		}
		// Each worker's first transaction follows its own setup.
		for i := 0; i < n; i++ {
			ready, begin := -1, -1
			for j, e := range after {
				if e == fmt.Sprintf("ready %d", i) && ready < 0 {
					ready = j
				}
				if e == fmt.Sprintf("begin %d", i) && begin < 0 {
					begin = j
				}
			}
			if ready < 0 || begin < ready {
				t.Fatalf("worker %d: ready at %d, first begin at %d", i, ready, begin)
			}
		}
		checkStartRule(t, g, true, true)
	}
}

// The in-memory ledger has no worker connections: setup_ns is null and the
// output is the same in both profiles.
func TestProfilesMemoryLedger(t *testing.T) {
	for _, p := range []Profile{Primary, Cold} {
		b := rtBroker(t)
		b.profile = p
		res, g := runGroup(t, b, groupOp(t, 8))
		rtWant(t, res, `{"outcomes":{"effect_completed":3,"replayed":5},"result":"concurrent"}`)
		checkStartRule(t, g, false, p == Cold)
	}
	_, g := runGroup(t, rtBroker(t), map[string]any{"op": "concurrent", "operations": []any{}})
	if g["ready_ns"] != int64(0) {
		t.Fatalf("empty group ready_ns %v", g["ready_ns"])
	}
}

// stderrOf runs f with os.Stderr captured.
func stderrOf(t *testing.T, f func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	saved := os.Stderr
	os.Stderr = w
	var buf bytes.Buffer
	done := make(chan struct{})
	go func() { io.Copy(&buf, r); close(done) }()
	f()
	os.Stderr = saved
	w.Close()
	<-done
	return buf.String()
}

// A worker whose connection cannot be opened answers LEDGER_UNAVAILABLE with
// no transaction and no audit; the other workers run normally; stderr has
// only the worker category and the code.
func TestWorkerSetupFailure(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("file permissions do not stop root")
	}
	// primary: only worker 0's setup fails (the file is unreadable during it).
	path := filepath.Join(t.TempDir(), "l.db")
	b, _ := sqlRT(t, path)
	b.trace = func(event string, worker int) {
		if worker != 0 {
			return
		}
		switch event {
		case "setup":
			os.Chmod(path, 0)
		case "ready":
			os.Chmod(path, 0o600)
		}
	}
	op := map[string]any{"op": "concurrent", "operations": []any{rtExecute(t, "a", ""), rtExecute(t, "b", "")}}
	var res, g map[string]any
	errs := stderrOf(t, func() { res, g = runGroup(t, b, op) })
	rtWant(t, res, `{"outcomes":{"LEDGER_UNAVAILABLE":1,"effect_completed":1},"result":"concurrent"}`)
	ws := g["operations"].([]any)
	if tx := ws[0].(map[string]any)["tx"].(map[string]any); len(tx) != 0 {
		t.Errorf("failed worker tx %v", tx)
	}
	if tx := ws[1].(map[string]any)["tx"].(map[string]any); len(tx) != 4 {
		t.Errorf("other worker tx %v", tx)
	}
	checkStartRule(t, g, true, false)
	if errs != "ledger error: worker SQLITE_CANTOPEN\n" {
		t.Errorf("stderr %q", errs)
	}
	rtWant(t, b.final(), `{"consumed_permits":1,"effects_applied":1,"grant_uses":{},"receipts":{"observed_success":1},"refusals":0}`)

	// cold: every setup fails after release.
	path = filepath.Join(t.TempDir(), "l.db")
	b, _ = sqlRT(t, path)
	b.profile = Cold
	if err := os.Chmod(path, 0); err != nil {
		t.Fatal(err)
	}
	errs = stderrOf(t, func() { res, g = runGroup(t, b, op) })
	os.Chmod(path, 0o600)
	rtWant(t, res, `{"outcomes":{"LEDGER_UNAVAILABLE":2},"result":"concurrent"}`)
	for _, w := range g["operations"].([]any) {
		if tx := w.(map[string]any)["tx"].(map[string]any); len(tx) != 0 {
			t.Errorf("failed worker tx %v", tx)
		}
	}
	checkStartRule(t, g, true, true)
	if errs != strings.Repeat("ledger error: worker SQLITE_CANTOPEN\n", 2) {
		t.Errorf("stderr %q", errs)
	}
	rtWant(t, b.final(), `{"consumed_permits":0,"effects_applied":0,"grant_uses":{},"receipts":{},"refusals":0}`)
}

// Setup verifies every setting by reading it back.
func TestWorkerSettingsReadBack(t *testing.T) {
	_, l := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
	w, err := l.fork()
	if err != nil {
		t.Fatal(err)
	}
	defer w.close()
	s := w.(*sqlLedger)
	if err := s.verifySettings(); err != nil {
		t.Fatal(err)
	}
	for _, set := range []string{"PRAGMA busy_timeout = 50", "PRAGMA synchronous = NORMAL", "PRAGMA foreign_keys = OFF",
		"PRAGMA trusted_schema = ON"} {
		if _, err := s.conn.ExecContext(ctx, set); err != nil {
			t.Fatal(err)
		}
		if err := s.verifySettings(); err != errSettings {
			t.Errorf("%s: verify %v", set, err)
		}
		for _, reset := range connSettings {
			s.conn.ExecContext(ctx, reset)
		}
		s.conn.ExecContext(ctx, "PRAGMA synchronous = FULL")
	}
	if err := s.verifySettings(); err != nil {
		t.Fatal(err)
	}
}
