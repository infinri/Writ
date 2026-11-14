package broker

import (
	"path/filepath"
	"reflect"
	"testing"
)

// D-026 (LEDGER-SQLITE.md "Per-operation timing"): every tx carries `error`,
// null without a database error, otherwise its code (busy or other) and
// where it was raised (begin, body, end), always with outcome rollback.

func txJSON(t *testing.T, tm *opTiming, name string) map[string]any {
	t.Helper()
	v, ok := tm.json()["tx"].(map[string]any)[name]
	if !ok {
		t.Fatalf("no %s transaction", name)
	}
	return v.(map[string]any)
}

func wantTxError(t *testing.T, tx map[string]any, want any) {
	t.Helper()
	if !reflect.DeepEqual(tx["error"], want) || tx["outcome"] != "rollback" {
		t.Fatalf("tx %v, want error %v with outcome rollback", tx, want)
	}
}

func TestTxErrorBusyBegin(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	_, l := sqlRT(t, path)
	release := holdLock(t, path)
	defer release()
	if _, err := l.conn.ExecContext(ctx, `PRAGMA busy_timeout = 50`); err != nil {
		t.Fatal(err)
	}
	tm := newOpTiming(map[string]any{"op": "x"})
	if err := l.audit(tm, "X"); err == nil {
		t.Fatal("audit began under a held write lock")
	}
	tx := txJSON(t, tm, "audit")
	wantTxError(t, tx, map[string]any{"code": "busy", "at": "begin"})
	if tx["body_ns"] != int64(0) || tx["end_ns"] != int64(0) {
		t.Fatalf("tx %v", tx)
	}
}

func TestTxErrorBody(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	_, l := sqlRT(t, path)
	if _, err := l.conn.ExecContext(ctx, `CREATE TEMP TRIGGER inj BEFORE INSERT ON audit_refusals BEGIN SELECT RAISE(ABORT, 'injected'); END`); err != nil {
		t.Fatal(err)
	}
	tm := newOpTiming(map[string]any{"op": "x"})
	if err := l.audit(tm, "X"); err == nil {
		t.Fatal("audit insert succeeded")
	}
	wantTxError(t, txJSON(t, tm, "audit"), map[string]any{"code": "other", "at": "body"})
}

func TestTxErrorCommit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, _ := sqlRT(t, path)
	execOn(t, path, `CREATE TABLE inj (p TEXT REFERENCES adapter_consumed_permits (permit_id) DEFERRABLE INITIALLY DEFERRED);
		CREATE TRIGGER inj AFTER INSERT ON adapter_effects BEGIN INSERT INTO inj VALUES ('absent'); END`)
	tm := newOpTiming(map[string]any{"op": "execute"})
	rtWant(t, b.applyOn(b.led, tm, rtExecute(t, "k1", ""), 0), unavailable)
	wantTxError(t, txJSON(t, tm, "adapter"), map[string]any{"code": "other", "at": "end"})
	for _, name := range []string{"reserve", "dispatch", "audit"} {
		if e := txJSON(t, tm, name)["error"]; e != nil {
			t.Errorf("%s error %v", name, e)
		}
	}
}

// Ordinary results and refusals (rollbacks included) raise no database
// error, in either ledger.
func TestTxErrorNullWithoutDatabaseError(t *testing.T) {
	ids := []string{"b-01-allow-path", "b-03-unsealed", "b-09-forged-permit", "b-15-grant-single-use",
		"b-30-idempotent-retry", "b-34-concurrent-duplicates", "b-35-crash-after-reserve-resumes"}
	var check func(string, map[string]any)
	check = func(where string, s map[string]any) {
		for name, v := range s["tx"].(map[string]any) {
			tx := v.(map[string]any)
			if e, ok := tx["error"]; !ok || e != nil {
				t.Errorf("%s %s: error %v (present %v)", where, name, e, ok)
			}
		}
		if ops, ok := s["operations"].([]any); ok {
			for _, o := range ops {
				check(where+" worker", o.(map[string]any))
			}
		}
	}
	for _, ledger := range []string{"memory", "sqlite"} {
		var store *SQLite
		if ledger == "sqlite" {
			store = &SQLite{Dir: t.TempDir()}
		}
		for _, id := range ids {
			for _, s := range timedSteps(t, id, store) {
				check(ledger+" "+id, s)
			}
		}
	}
}
