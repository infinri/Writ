package broker

import (
	"bytes"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/mattn/go-sqlite3"

	"writ.dev/m1/proto/internal/sjson"
)

// D-025 (LEDGER-SQLITE.md "Storage failures" rules 1 and 4 to 6, S-1, S-2,
// C-3, C-4, A, B), Go unit tests.

const unavailable = `{"code":"LEDGER_UNAVAILABLE","result":"refused"}`

func sqlRT(t *testing.T, path string) (*Broker, *sqlLedger) {
	t.Helper()
	b := rtBroker(t)
	l, err := (&SQLite{DB: path}).open()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.close)
	b.led = l
	return b, l
}

func names(t *testing.T, dir string) string {
	t.Helper()
	es, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var n []string
	for _, e := range es {
		n = append(n, e.Name())
	}
	sort.Strings(n)
	return strings.Join(n, ",")
}

// durable is every table the broker writes, row by row.
func durable(t *testing.T, path string) string {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var out strings.Builder
	for _, table := range tables {
		rows, err := db.Query(`SELECT * FROM ` + table + ` ORDER BY 1`)
		if err != nil {
			t.Fatal(err)
		}
		cols, _ := rows.Columns()
		for rows.Next() {
			vals := make([]any, len(cols))
			ptrs := make([]any, len(cols))
			for i := range vals {
				ptrs[i] = &vals[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				t.Fatal(err)
			}
			fmt.Fprintf(&out, "%s %v\n", table, vals)
		}
		rows.Close()
	}
	return out.String()
}

// S-1: a non-WAL file that fails verification is not changed at all (never
// switched to WAL) and gets no sidecar file.
func TestRejectedNonWALFileUnchanged(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "foreign.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(Schema + `UPDATE meta SET value = '2' WHERE key = 'schema_version';`); err != nil {
		t.Fatal(err)
	}
	var mode string
	if err := db.QueryRow(`PRAGMA journal_mode`).Scan(&mode); err != nil || mode != "delete" {
		t.Fatalf("journal mode %q %v", mode, err)
	}
	db.Close()
	before, _ := os.ReadFile(path)
	rtWant(t, scenarioOn(t, path), `{"error":"LEDGER_UNAVAILABLE"}`)
	if after, _ := os.ReadFile(path); !bytes.Equal(before, after) {
		t.Fatal("rejected file modified")
	}
	if n := names(t, dir); n != "foreign.db" {
		t.Fatalf("directory: %s", n)
	}

	// The same file with a valid version is accepted and switched to WAL.
	execOn(t, path, `UPDATE meta SET value = '1' WHERE key = 'schema_version'`)
	if out := scenarioOn(t, path); out["steps"] == nil {
		t.Fatalf("valid ledger refused: %s", sjson.Encode(out))
	}
	db, _ = sql.Open("sqlite3", path)
	defer db.Close()
	if err := db.QueryRow(`PRAGMA journal_mode`).Scan(&mode); err != nil || mode != "wal" {
		t.Fatalf("journal mode %q %v", mode, err)
	}
}

// S-2: a database file this run created is removed when initialization
// fails; a pre-existing path (here a directory where the WAL file would be)
// is kept, and a pre-existing file is never removed.
func TestCreatedFileRemovedOnInitFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "new.db")
	if err := os.Mkdir(path+"-wal", 0o700); err != nil {
		t.Fatal(err)
	}
	rtWant(t, scenarioOn(t, path), `{"error":"LEDGER_UNAVAILABLE"}`)
	if n := names(t, dir); n != "new.db-wal" {
		t.Fatalf("directory: %s", n)
	}

	garbage := filepath.Join(dir, "garbage.db")
	if err := os.WriteFile(garbage, bytes.Repeat([]byte("x"), 4096), 0o600); err != nil {
		t.Fatal(err)
	}
	rtWant(t, scenarioOn(t, garbage), `{"error":"LEDGER_UNAVAILABLE"}`)
	if b, _ := os.ReadFile(garbage); !bytes.Equal(b, bytes.Repeat([]byte("x"), 4096)) {
		t.Fatal("pre-existing file changed")
	}
}

// Rule 4: diagnostics carry only a category and a primary code name.
func TestDiagnosticSanitized(t *testing.T) {
	_, pathErr := os.Open("/nonexistent-secret-dir/l.db")
	for err, want := range map[error]string{
		pathErr:                                  "open io",
		tagged("initialize", errSchema):          "initialize io",
		txErr("adapter", sqlite3.Error{Code: 5}): "transaction adapter SQLITE_BUSY",
		tagged("final", fmt.Errorf("wrap /x/y.db: %w", sqlite3.Error{Code: sqlite3.ErrCorrupt})): "final SQLITE_CORRUPT",
		txErr("reserve", errGrantIntegrity): "transaction reserve io",
	} {
		if got := Diagnostic(err); got != want {
			t.Errorf("%v: %q, want %q", err, got, want)
		}
	}
	// A real open failure in a missing directory.
	_, err := (&SQLite{DB: filepath.Join(t.TempDir(), "secret-dir", "l.db")}).open()
	if d := Diagnostic(err); d != "open io" {
		t.Fatalf("missing directory: %q", d)
	}
}

// C-4: a stored grant failing integrity is LEDGER_UNAVAILABLE inside
// T-reserve (rolled back: no use, no receipt); a well-formed different
// binding stays GRANT_MISMATCH.
func TestStoredGrantIntegrity(t *testing.T) {
	f := loadRace(t)
	replace := func(b, old, new string) string { return strings.Replace(b, old, new, 1) }
	for name, c := range map[string]struct {
		mutate func(string) string
		want   string
	}{
		"unparsable":       {func(b string) string { return b[:len(b)-5] }, unavailable},
		"wrong members":    {func(string) string { return `{"verdict":"approved"}` }, unavailable},
		"extra member":     {func(b string) string { return b[:len(b)-1] + `,"zz":"x"}` }, unavailable},
		"duplicate member": {func(b string) string { return b[:len(b)-1] + `,"verdict":"approved"}` }, unavailable},
		"not canonical":    {func(b string) string { return replace(b, `","`, `", "`) }, unavailable},
		"wrong type": {func(b string) string {
			return replace(b, `"requested_capabilities":[`, `"requested_capabilities":["x",1,`)
		}, unavailable},
		"not an object":    {func(string) string { return `[]` }, unavailable},
		"different tenant": {func(b string) string { return replace(b, `"tenant_id":"`, `"tenant_id":"other-`) }, `{"code":"GRANT_MISMATCH","result":"refused"}`},
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "l.db")
			b := sqlBroker(t, f.initial, &SQLite{DB: path})
			rtWant(t, b.apply(f.approve, 0), `{"result":"grant_issued"}`)
			db, err := sql.Open("sqlite3", path)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			var binding string
			if err := db.QueryRow(`SELECT binding FROM grants`).Scan(&binding); err != nil {
				t.Fatal(err)
			}
			if !bindingValid(binding) {
				t.Fatal("issued binding fails integrity")
			}
			bad := c.mutate(binding)
			if bad == binding {
				t.Fatal("mutation did nothing")
			}
			if _, err := db.Exec(`UPDATE grants SET binding = ?`, bad); err != nil {
				t.Fatal(err)
			}
			rtWant(t, b.apply(withKey(t, f.initial, f.approval, "c4", ""), 1), c.want)
			for q, want := range map[string]int64{
				`SELECT uses FROM grants`:              0,
				`SELECT count(*) FROM grant_uses`:      0,
				`SELECT count(*) FROM receipts`:        0,
				`SELECT count(*) FROM receipt_events`:  0,
				`SELECT count(*) FROM adapter_effects`: 0,
				`SELECT count(*) FROM audit_refusals`:  1,
			} {
				if got := count(t, path, q); got != want {
					t.Errorf("%s: %d, want %d", q, got, want)
				}
			}
		})
	}
}

// D-025 A, C-3: resuming a `reserved` receipt commits T-reserve without any
// write and without reading or using the grant again.
func TestResumeReservedWritesNothing(t *testing.T) {
	f := loadRace(t)
	path := filepath.Join(t.TempDir(), "l.db")
	b := sqlBroker(t, f.initial, &SQLite{DB: path})
	l := b.led.(*sqlLedger)
	op := withKey(t, f.initial, f.approval, "a1", "crash_after_reserve")
	rtWant(t, b.apply(f.approve, 0), `{"result":"grant_issued"}`)
	rtWant(t, b.apply(op, 1), `{"result":"crashed"}`)
	before := durable(t, path)
	changes := func() (n int64) {
		if err := l.conn.QueryRowContext(ctx, `SELECT total_changes()`).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	c0 := changes()
	req := op["request"].(map[string]any)
	tm := newOpTiming(map[string]any{"op": "execute"})
	outcome, err := l.reserve(tm, reservation{
		tenant: req["tenant_id"].(string), key: "a1", grantLabel: "g1", approval: true,
		fingerprint: requestFingerprint(req),
		check:       func(*grant) string { t.Error("grant checked on resume"); return "" },
	})
	if err != nil || outcome != "" || !tm.tx["reserve"].commit {
		t.Fatalf("resume: %q %v %+v", outcome, err, tm.tx["reserve"])
	}
	if c := changes(); c != c0 {
		t.Fatalf("T-reserve changed %d rows on resume", c-c0)
	}
	if durable(t, path) != before {
		t.Fatal("durable state changed by T-reserve on resume")
	}
	// The full retry completes with exactly one grant use.
	rtWant(t, b.apply(withKey(t, f.initial, f.approval, "a1", ""), 2), `{"result":"effect_completed"}`)
	if n := count(t, path, `SELECT count(*) FROM grant_uses`); n != 1 {
		t.Fatalf("grant_uses %d", n)
	}
}

// D-025 B: T-adapter that cannot begin answers LEDGER_UNAVAILABLE before any
// permit check (a tampered permit included).
func TestAdapterBeginFailureTakesPrecedence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, l := sqlRT(t, path)
	first := rtExecute(t, "k1", "")
	rtWant(t, b.apply(first, 0), `{"result":"effect_completed"}`)
	other := holdLock(t, path)
	if _, err := l.conn.ExecContext(ctx, `PRAGMA busy_timeout = 50`); err != nil {
		t.Fatal(err)
	}
	wrong := rtExecute(t, "k9", "")["request"]
	tm := newOpTiming(map[string]any{"op": "present_permit"})
	rtWant(t, b.applyOn(l, tm, map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"], "tamper": "mac"}, 1), unavailable)
	if a := tm.tx["adapter"]; a.commit || a.body != 0 || a.end != 0 {
		t.Fatalf("adapter transaction ran: %+v", a)
	}
	rtWant(t, b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": wrong, "tamper": "expiry"}, 2), unavailable)
	rtWant(t, b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"]}, 3), unavailable)
	other()
	if _, err := l.conn.ExecContext(ctx, `PRAGMA busy_timeout = 5000`); err != nil {
		t.Fatal(err)
	}
	// The audit rows could not be written either (lock held).
	rtWant(t, b.final(), `{"consumed_permits":1,"effects_applied":1,"grant_uses":{},"receipts":{"observed_success":1},"refusals":0}`)
}

func holdLock(t *testing.T, path string) (release func()) {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	c, err := db.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.ExecContext(ctx, `BEGIN IMMEDIATE`); err != nil {
		t.Fatal(err)
	}
	return func() {
		c.ExecContext(ctx, `ROLLBACK`)
		c.Close()
		db.Close()
	}
}

// D-025 B: inside T-adapter the order is fingerprint, MAC, expiry, replay;
// only a fully verified permit inserts the consumed id and the effect, and a
// failing COMMIT leaves neither.
func TestAdapterOrderAndCommitFailure(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, _ := sqlRT(t, path)
	// A deferred foreign-key violation inserted with the effect row makes
	// T-adapter's COMMIT fail.
	execOn(t, path, `CREATE TABLE inj (p TEXT REFERENCES adapter_consumed_permits (permit_id) DEFERRABLE INITIALLY DEFERRED);
		CREATE TRIGGER inj AFTER INSERT ON adapter_effects BEGIN INSERT INTO inj VALUES ('absent'); END`)
	first := rtExecute(t, "k1", "")
	tm := newOpTiming(map[string]any{"op": "execute"})
	rtWant(t, b.applyOn(b.led, tm, first, 0), unavailable)
	if a, ok := tm.tx["adapter"]; !ok || a.commit {
		t.Fatalf("adapter transaction: %+v", a)
	}
	state := func(consumed, effects int64) {
		t.Helper()
		if c, e := count(t, path, `SELECT count(*) FROM adapter_consumed_permits`), count(t, path, `SELECT count(*) FROM adapter_effects`); c != consumed || e != effects {
			t.Fatalf("consumed %d effects %d, want %d %d", c, e, consumed, effects)
		}
	}
	state(0, 0)
	if n := count(t, path, `SELECT count(*) FROM receipts WHERE status = 'dispatched'`); n != 1 {
		t.Fatalf("dispatched receipts %d", n)
	}
	execOn(t, path, `DROP TRIGGER inj`)

	present := func(step int, request any, tamper string) map[string]any {
		op := map[string]any{"op": "present_permit", "of_step": int64(0), "request": request}
		if tamper != "" {
			op["tamper"] = tamper
		}
		return b.apply(op, step)
	}
	wrong := rtExecute(t, "k9", "")["request"]
	req := first["request"]
	// Unconsumed permit: each refusal leaves nothing.
	rtWant(t, present(1, wrong, "mac"), `{"code":"PERMIT_REQUEST_MISMATCH","result":"refused"}`)
	rtWant(t, present(2, req, "mac"), `{"code":"PERMIT_INVALID","result":"refused"}`)
	rtWant(t, present(3, req, "expiry"), `{"code":"PERMIT_INVALID","result":"refused"}`)
	state(0, 0)
	rtWant(t, present(4, req, ""), `{"result":"effect_applied"}`)
	state(1, 1)
	rtWant(t, present(5, req, ""), `{"code":"PERMIT_REPLAYED","result":"refused"}`)
	rtWant(t, b.apply(map[string]any{"op": "advance_time", "now": "2026-11-07T12:00:00Z"}, 6), `{"result":"time_advanced"}`)
	// Expired and consumed: expiry before replay; MAC before expiry;
	// fingerprint before MAC.
	rtWant(t, present(7, req, ""), `{"code":"PERMIT_EXPIRED","result":"refused"}`)
	rtWant(t, present(8, req, "mac"), `{"code":"PERMIT_INVALID","result":"refused"}`)
	rtWant(t, present(9, wrong, "mac"), `{"code":"PERMIT_REQUEST_MISMATCH","result":"refused"}`)
	state(1, 1)
}
