package broker

import (
	"bytes"
	"database/sql"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// D-027 and D-028 (LEDGER-SQLITE.md "Concurrency profiles and release
// protocol"), Go unit tests with an event trace and a bounded wait: every
// worker exists and has signalled ready before release; in primary every
// connection setup (read-back included) finished before release and no
// transaction began or is open before release; in cold every setup and
// every BEGIN follows release; a failing setup in either profile neither
// deadlocks the group nor affects the other workers.

const groupDeadline = 60 * time.Second

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
	l.mu.Lock()
	defer l.mu.Unlock()
	i := slices.Index(l.events, "release -1")
	if i < 0 {
		t.Fatalf("no release in %v", l.events)
	}
	return l.events[:i], l.events[i+1:]
}

// each reports whether events hold kind for every worker 0..n-1.
func each(events []string, kind string, n int) bool {
	for i := 0; i < n; i++ {
		if !slices.Contains(events, fmt.Sprintf("%s %d", kind, i)) {
			return false
		}
	}
	return true
}

func none(events []string, kind string) bool {
	for _, e := range events {
		if strings.HasPrefix(e, kind+" ") {
			return false
		}
	}
	return true
}

func groupOp(t *testing.T, n int) map[string]any {
	ops := make([]any, n)
	for i := range ops {
		ops[i] = rtExecute(t, fmt.Sprintf("k%d", i%3), "") // shared keys: replays and key-lock waits too
	}
	return map[string]any{"op": "concurrent", "operations": ops}
}

// runGroup runs one timed concurrent group on b within groupDeadline and
// returns its result, its timing and any panic it raised.
func runGroup(t *testing.T, b *Broker, op map[string]any) (map[string]any, map[string]any, any) {
	t.Helper()
	type out struct {
		res, timing map[string]any
		panicked    any
	}
	ch := make(chan out, 1)
	go func() {
		var o out
		defer func() { o.panicked = recover(); ch <- o }()
		tm := newOpTiming(op)
		started := time.Now()
		tm.setDispatch(started)
		o.res = b.applyOn(b.led, tm, op, 0)
		tm.setTotal(time.Since(started))
		o.timing = tm.json()
	}()
	select {
	case o := <-ch:
		return o.res, o.timing, o.panicked
	case <-time.After(groupDeadline):
		t.Fatal("concurrent group did not finish (deadlock)")
		return nil, nil, nil
	}
}

func mustRun(t *testing.T, b *Broker, op map[string]any) (map[string]any, map[string]any) {
	t.Helper()
	res, g, p := runGroup(t, b, op)
	if p != nil {
		t.Fatalf("panic %v", p)
	}
	return res, g
}

func i64(t *testing.T, m map[string]any, k string) int64 {
	t.Helper()
	v, ok := m[k].(int64)
	if !ok {
		t.Fatalf("%s is %v", k, m[k])
	}
	return v
}

// checkTiming is the D-028 integrity rule for one group, including the
// reported release_delay_ns and start_skew_ns.
func checkTiming(t *testing.T, g map[string]any, sqlite, cold bool) {
	t.Helper()
	ready, release, total := i64(t, g, "ready_ns"), i64(t, g, "release_ns"), i64(t, g, "total_ns")
	if !(0 <= ready && ready <= release && release <= total) {
		t.Fatalf("ready %d release %d total %d", ready, release, total)
	}
	var starts []int64
	for _, o := range g["operations"].([]any) {
		w := o.(map[string]any)
		at, start, wt := i64(t, w, "ready_at_ns"), i64(t, w, "start_ns"), i64(t, w, "total_ns")
		starts = append(starts, start)
		if at > ready || start < release || start+wt > total {
			t.Errorf("worker ready_at %d start %d total %d; group ready %d release %d total %d", at, start, wt, ready, release, total)
		}
		if d := i64(t, w, "release_delay_ns"); d != start-release {
			t.Errorf("release_delay_ns %d, start %d release %d", d, start, release)
		}
		if !sqlite {
			if w["setup_ns"] != nil {
				t.Errorf("memory setup_ns %v", w["setup_ns"])
			}
			continue
		}
		setup := i64(t, w, "setup_ns")
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
		if cold && parts+setup > wt || !cold && (setup > at || parts > wt) {
			t.Errorf("cold %v: setup %d parts %d total %d ready_at %d", cold, setup, parts, wt, at)
		}
	}
	skew := int64(0)
	if len(starts) > 1 {
		skew = slices.Max(starts) - slices.Min(starts)
	}
	if got := i64(t, g, "start_skew_ns"); got != skew {
		t.Errorf("start_skew_ns %d, want %d", got, skew)
	}
}

// noTransactionHeld checks from an outside connection, without waiting, that
// no connection holds the write lock and none pins the WAL (a read
// transaction would block a truncating checkpoint).
func noTransactionHeld(t *testing.T, path string) error {
	db, err := sql.Open("sqlite3", path+"?_busy_timeout=0")
	if err != nil {
		return err
	}
	defer db.Close()
	c, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer c.Close()
	if _, err := c.ExecContext(ctx, `BEGIN IMMEDIATE`); err != nil {
		return fmt.Errorf("write lock held at release: %w", err)
	}
	if _, err := c.ExecContext(ctx, `ROLLBACK`); err != nil {
		return err
	}
	var busy, logN, ckpt int64
	if err := c.QueryRowContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`).Scan(&busy, &logN, &ckpt); err != nil {
		return err
	}
	if busy != 0 {
		return fmt.Errorf("a transaction is open at release")
	}
	return nil
}

func TestPrimaryReleaseProtocol(t *testing.T) {
	for _, n := range []int{16, 16, 16, 64} {
		path := filepath.Join(t.TempDir(), "l.db")
		b, _ := sqlRT(t, path)
		var log traceLog
		var probe error
		b.trace = func(event string, worker int) {
			log.add(event, worker)
			if event == "release" {
				probe = noTransactionHeld(t, path)
			}
		}
		res, g := mustRun(t, b, groupOp(t, n))
		rtWant(t, res, fmt.Sprintf(`{"outcomes":{"effect_completed":3,"replayed":%d},"result":"concurrent"}`, n-3))
		if probe != nil {
			t.Fatal(probe)
		}
		before, after := log.split(t)
		for _, k := range []string{"created", "setup", "setup_done", "ready"} {
			if !each(before, k, n) {
				t.Fatalf("%d workers: not every %q before release: %v", n, k, before)
			}
		}
		if !none(before, "begin") || !none(before, "start") {
			t.Fatalf("operation before release: %v", before)
		}
		if !none(after, "setup") || !each(after, "start", n) || !each(after, "begin", n) {
			t.Fatalf("after release: %v", after)
		}
		checkTiming(t, g, true, false)
	}
}

func TestColdReleaseProtocol(t *testing.T) {
	for _, n := range []int{16, 16, 16, 64} {
		path := filepath.Join(t.TempDir(), "l.db")
		b, _ := sqlRT(t, path)
		b.profile = Cold
		var log traceLog
		b.trace = log.add
		res, g := mustRun(t, b, groupOp(t, n))
		rtWant(t, res, fmt.Sprintf(`{"outcomes":{"effect_completed":3,"replayed":%d},"result":"concurrent"}`, n-3))
		before, after := log.split(t)
		if !each(before, "created", n) || !each(before, "ready", n) {
			t.Fatalf("not every worker ready before release: %v", before)
		}
		if !none(before, "setup") || !none(before, "begin") || !none(before, "start") {
			t.Fatalf("setup or operation before release: %v", before)
		}
		if !each(after, "setup_done", n) {
			t.Fatalf("after release: %v", after)
		}
		// Each worker passes the gate, then sets up, then begins.
		for i := 0; i < n; i++ {
			s := slices.Index(after, fmt.Sprintf("start %d", i))
			d := slices.Index(after, fmt.Sprintf("setup_done %d", i))
			bg := slices.Index(after, fmt.Sprintf("begin %d", i))
			if s < 0 || d < s || bg < d {
				t.Fatalf("worker %d: start %d setup_done %d begin %d", i, s, d, bg)
			}
		}
		checkTiming(t, g, true, true)
	}
}

// The in-memory ledger has no worker connections but the same protocol.
func TestReleaseProtocolMemoryLedger(t *testing.T) {
	for _, p := range []Profile{Primary, Cold} {
		b := rtBroker(t)
		b.profile = p
		var log traceLog
		b.trace = log.add
		res, g := mustRun(t, b, groupOp(t, 8))
		rtWant(t, res, `{"outcomes":{"effect_completed":3,"replayed":5},"result":"concurrent"}`)
		before, after := log.split(t)
		if !each(before, "ready", 8) || !none(before, "start") || !each(after, "start", 8) || !none(before, "setup") {
			t.Fatalf("trace %v | %v", before, after)
		}
		checkTiming(t, g, false, p == Cold)
	}
	_, g := mustRun(t, rtBroker(t), map[string]any{"op": "concurrent", "operations": []any{}})
	if g["ready_ns"] != int64(0) || g["release_ns"] != int64(0) || g["start_skew_ns"] != int64(0) {
		t.Fatalf("empty group %v", g)
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

func noTx(t *testing.T, w any) {
	t.Helper()
	if tx := w.(map[string]any)["tx"].(map[string]any); len(tx) != 0 {
		t.Errorf("failed worker tx %v", tx)
	}
}

// A worker whose connection cannot be opened answers LEDGER_UNAVAILABLE with
// no transaction and no audit, still reports ready and passes the gate; the
// group finishes and the other workers run normally; stderr has only the
// worker category and the code.
func TestWorkerSetupFailure(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("file permissions do not stop root")
	}
	op := map[string]any{"op": "concurrent", "operations": []any{rtExecute(t, "a", ""), rtExecute(t, "b", ""), rtExecute(t, "c", "")}}
	line := "ledger error: worker SQLITE_CANTOPEN\n"
	for _, p := range []Profile{Primary, Cold} {
		// Only worker 0's setup fails: the test serializes setups through the
		// trace and makes the file unreadable during worker 0's.
		path := filepath.Join(t.TempDir(), "l.db")
		b, _ := sqlRT(t, path)
		b.profile = p
		var mu sync.Mutex
		var log traceLog
		b.trace = func(event string, worker int) {
			log.add(event, worker)
			switch event {
			case "setup":
				mu.Lock()
				if worker == 0 {
					os.Chmod(path, 0)
				}
			case "setup_done":
				if worker == 0 {
					os.Chmod(path, 0o600)
				}
				mu.Unlock()
			}
		}
		var res, g map[string]any
		errs := stderrOf(t, func() { res, g = mustRun(t, b, op) })
		rtWant(t, res, `{"outcomes":{"LEDGER_UNAVAILABLE":1,"effect_completed":2},"result":"concurrent"}`)
		ws := g["operations"].([]any)
		noTx(t, ws[0])
		for _, w := range ws[1:] {
			if tx := w.(map[string]any)["tx"].(map[string]any); len(tx) != 4 {
				t.Errorf("profile %d: other worker tx %v", p, tx)
			}
		}
		before, after := log.split(t)
		if !each(before, "ready", 3) || !each(after, "start", 3) {
			t.Errorf("profile %d: trace %v | %v", p, before, after)
		}
		checkTiming(t, g, true, p == Cold)
		if errs != line {
			t.Errorf("profile %d: stderr %q", p, errs)
		}
		rtWant(t, b.final(), `{"consumed_permits":2,"effects_applied":2,"grant_uses":{},"receipts":{"observed_success":2},"refusals":0}`)

		// Every setup fails.
		path = filepath.Join(t.TempDir(), "l.db")
		b, _ = sqlRT(t, path)
		b.profile = p
		if err := os.Chmod(path, 0); err != nil {
			t.Fatal(err)
		}
		errs = stderrOf(t, func() { res, g = mustRun(t, b, op) })
		os.Chmod(path, 0o600)
		rtWant(t, res, `{"outcomes":{"LEDGER_UNAVAILABLE":3},"result":"concurrent"}`)
		for _, w := range g["operations"].([]any) {
			noTx(t, w)
		}
		checkTiming(t, g, true, p == Cold)
		if errs != strings.Repeat(line, 3) {
			t.Errorf("profile %d: stderr %q", p, errs)
		}
		rtWant(t, b.final(), `{"consumed_permits":0,"effects_applied":0,"grant_uses":{},"receipts":{},"refusals":0}`)
	}
}

// A worker that panics before signalling ready never blocks the group: the
// panic reaches the coordinator after every worker finished.
func TestWorkerPanicBeforeReadyDoesNotDeadlock(t *testing.T) {
	b, _ := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
	b.trace = func(event string, worker int) {
		if event == "setup" && worker == 0 {
			panic("injected")
		}
	}
	if _, _, p := runGroup(t, b, groupOp(t, 4)); p != "injected" {
		t.Fatalf("panic %v", p)
	}
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

// The release-time probe detects a held write lock and an open read
// transaction.
func TestNoTransactionHeldProbe(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	_, l := sqlRT(t, path)
	if err := noTransactionHeld(t, path); err != nil {
		t.Fatal(err)
	}
	release := holdLock(t, path)
	if noTransactionHeld(t, path) == nil {
		t.Error("held write lock not detected")
	}
	release()
	if _, err := l.conn.ExecContext(ctx, `INSERT INTO audit_refusals (code) VALUES ('X')`); err != nil {
		t.Fatal(err)
	}
	tx, err := l.conn.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if noTransactionHeld(t, path) == nil {
		t.Error("open transaction not detected")
	}
}
