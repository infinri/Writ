package broker

import (
	"bufio"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mattn/go-sqlite3"

	"writ.dev/m1/proto/internal/sjson"
)

// LEDGER-SQLITE.md "Required tests" for the Go prototype.

func readCases(t *testing.T, set string) []map[string]any {
	t.Helper()
	f, err := os.Open("../../../fixtures/sets/" + set + ".jsonl")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var cases []map[string]any
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		v, perr := sjson.ParseUnlimited(sc.Text())
		if perr != nil {
			t.Fatal(perr)
		}
		cases = append(cases, v.(map[string]any))
	}
	return cases
}

func fixtureCase(t *testing.T, id string) map[string]any {
	t.Helper()
	for _, c := range readCases(t, "broker-scenarios") {
		if c["id"] == id {
			return c["input"].(map[string]any)
		}
	}
	t.Fatalf("fixture %s not found", id)
	return nil
}

func expected(c map[string]any) []string {
	expect := c["expect"].(map[string]any)
	alts := []any{expect}
	if c["compare"] == "one_of" {
		alts = expect["one_of"].([]any)
	}
	out := make([]string, len(alts))
	for i, a := range alts {
		out[i] = string(sjson.Encode(a))
	}
	return out
}

// 5. Schema drift.
func TestSchemaMatchesFixture(t *testing.T) {
	want, err := os.ReadFile("../../../fixtures/broker/ledger.sql")
	if err != nil {
		t.Fatal(err)
	}
	if Schema != string(want) {
		t.Fatal("embedded ledger.sql differs from m1/fixtures/broker/ledger.sql")
	}
}

// 1. Every broker fixture gives exactly the expected output on SQLite, with
// a new database file per scenario.
func TestSQLiteFixtures(t *testing.T) {
	dir := t.TempDir()
	store := &SQLite{Dir: dir}
	n := 0
	for _, set := range []string{"broker-scenarios", "protocol-broker"} {
		for _, c := range readCases(t, set) {
			got := string(sjson.Encode(RunWith(c["input"], store)))
			ok := false
			for _, w := range expected(c) {
				ok = ok || w == got
			}
			if !ok {
				t.Errorf("%s: got %s", c["id"], got)
			}
			n++
		}
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.db"))
	if n != 55 || len(files) != 49 { // 6 protocol-broker cases are INPUT_MALFORMED: nothing runs
		t.Fatalf("%d cases, %d database files", n, len(files))
	}
}

func sqlBroker(t *testing.T, initial map[string]any, store *SQLite) *Broker {
	t.Helper()
	b := newBroker(initial)
	led, err := store.open()
	if err != nil {
		t.Fatal(err)
	}
	b.led = led
	t.Cleanup(led.close)
	return b
}

func count(t *testing.T, path, query string) int64 {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var n int64
	if err := db.QueryRow(query).Scan(&n); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
	return n
}

// withKey returns a copy of an execute operation for another idempotency
// key, resealed under the fixture ingress key.
func withKey(t *testing.T, initial, op map[string]any, key, fault string) map[string]any {
	t.Helper()
	o := sjson.Clone(op).(map[string]any)
	req := o["request"].(map[string]any)
	req["idempotency_key"] = key
	seal := o["seal"].(map[string]any)
	k := hexKey(initial["ingress_keys"].(map[string]any)[seal["adapter_id"].(string)].(string))
	seal["mac"] = mac(k, sjson.MustCanonical(req))
	delete(o, "fault")
	if fault != "" {
		o["fault"] = fault
	}
	return o
}

type raceFixture struct {
	initial  map[string]any
	approve  map[string]any // grant g1, max_uses 1
	approval map[string]any // execute requiring g1
	allow    map[string]any // execute allowed without approval
}

func loadRace(t *testing.T) raceFixture {
	g := fixtureCase(t, "b-27-grant-race-16")
	d := fixtureCase(t, "b-34-concurrent-duplicates")
	gs, ds := g["steps"].([]any), d["steps"].([]any)
	return raceFixture{
		initial:  g["initial"].(map[string]any),
		approve:  gs[0].(map[string]any),
		approval: gs[1].(map[string]any)["operations"].([]any)[0].(map[string]any),
		allow:    ds[0].(map[string]any)["operations"].([]any)[0].(map[string]any),
	}
}

// Every connection gets the settings, and a write transaction takes the
// write lock at BEGIN (BEGIN IMMEDIATE), before any statement.
func TestConnectionSettingsAndBeginImmediate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	l, err := (&SQLite{DB: path}).open()
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	var journal string
	var sync, busy, fk, trusted int64
	for q, dest := range map[string]any{"PRAGMA journal_mode": &journal, "PRAGMA synchronous": &sync,
		"PRAGMA busy_timeout": &busy, "PRAGMA foreign_keys": &fk, "PRAGMA trusted_schema": &trusted} {
		if err := l.conn.QueryRowContext(ctx, q).Scan(dest); err != nil {
			t.Fatal(err)
		}
	}
	if journal != "wal" || sync != 2 || busy != 5000 || fk != 1 || trusted != 0 {
		t.Fatalf("settings: %s %d %d %d %d", journal, sync, busy, fk, trusted)
	}
	tx, err := l.conn.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	other, err := sql.Open("sqlite3", path+"?_busy_timeout=0")
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	_, err = other.Exec(`BEGIN IMMEDIATE`)
	if e, ok := err.(sqlite3.Error); !ok || e.Code != sqlite3.ErrBusy {
		t.Fatalf("second writer during an open transaction: %v, want SQLITE_BUSY", err)
	}
}

// 2. Atomicity: a failing statement inside T-reserve (the reserve event,
// written last) leaves no receipt, event or grant use.
func TestReserveAtomic(t *testing.T) {
	f := loadRace(t)
	path := filepath.Join(t.TempDir(), "l.db")
	b := sqlBroker(t, f.initial, &SQLite{DB: path})
	rtWant(t, b.apply(f.approve, 0), `{"result":"grant_issued"}`)
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TRIGGER injected BEFORE INSERT ON receipt_events WHEN NEW.cause = 'reserve'
		BEGIN SELECT RAISE(ABORT, 'injected failure'); END`); err != nil {
		t.Fatal(err)
	}
	rtWant(t, b.apply(withKey(t, f.initial, f.approval, "atomic", ""), 1), `{"code":"LEDGER_UNAVAILABLE","result":"refused"}`)
	for q, want := range map[string]int64{
		`SELECT count(*) FROM receipts`:       0,
		`SELECT count(*) FROM receipt_events`: 0,
		`SELECT count(*) FROM grant_uses`:     0,
		`SELECT uses FROM grants`:             0,
		`SELECT count(*) FROM audit_refusals`: 1,
	} {
		if got := count(t, path, q); got != want {
			t.Errorf("%s: %d, want %d", q, got, want)
		}
	}
	if _, err := db.Exec(`DROP TRIGGER injected`); err != nil {
		t.Fatal(err)
	}
	rtWant(t, b.apply(withKey(t, f.initial, f.approval, "atomic", ""), 2), `{"result":"effect_completed"}`)
	rtWant(t, b.final(), `{"consumed_permits":1,"effects_applied":1,"grant_uses":{"g1":1},"receipts":{"observed_success":1},"refusals":1}`)
}

// 3. Persistence across restart_broker (a new connection to the same file).
func TestSQLiteRestartKeepsLedger(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b := rtBroker(t)
	led, err := (&SQLite{DB: path}).open()
	if err != nil {
		t.Fatal(err)
	}
	defer led.close()
	b.led = led
	first := rtExecute(t, "k1", "")
	rtWant(t, b.apply(first, 0), `{"result":"effect_completed"}`)
	rtWant(t, b.apply(rtExecute(t, "k2", "crash_after_dispatch"), 1), `{"result":"crashed"}`)
	before := led.conn
	rtWant(t, b.apply(map[string]any{"op": "restart_broker"}, 2), `{"result":"restarted"}`)
	if led.conn == before || b.policy == nil {
		t.Fatal("restart must open a new connection and reload the policy")
	}
	rtWant(t, b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"]}, 3),
		`{"code":"PERMIT_REPLAYED","result":"refused"}`)
	rtWant(t, b.apply(rtExecute(t, "k1", ""), 4), `{"replayed":true,"result":"effect_completed"}`)
	rtWant(t, b.apply(rtExecute(t, "k2", ""), 5), `{"code":"RECONCILIATION_REQUIRED","result":"refused"}`)
	rtWant(t, b.apply(rtExecute(t, "k3", ""), 6), `{"result":"effect_completed"}`)
	rtWant(t, b.final(), `{"consumed_permits":3,"effects_applied":3,"grant_uses":{},"receipts":{"dispatched":1,"observed_success":2},"refusals":2}`)
	if n := count(t, path, `SELECT value FROM counters WHERE name = 'next_permit'`); n != 3 {
		t.Fatalf("next_permit %d", n)
	}

	// crash_after_reserve resumes after restart without a second grant use.
	f := loadRace(t)
	path = filepath.Join(t.TempDir(), "g.db")
	g := sqlBroker(t, f.initial, &SQLite{DB: path})
	rtWant(t, g.apply(f.approve, 0), `{"result":"grant_issued"}`)
	rtWant(t, g.apply(withKey(t, f.initial, f.approval, "r1", "crash_after_reserve"), 1), `{"result":"crashed"}`)
	rtWant(t, g.apply(map[string]any{"op": "restart_broker"}, 2), `{"result":"restarted"}`)
	rtWant(t, g.apply(withKey(t, f.initial, f.approval, "r1", ""), 3), `{"result":"effect_completed"}`)
	rtWant(t, g.final(), `{"consumed_permits":1,"effects_applied":1,"grant_uses":{"g1":1},"receipts":{"observed_success":1},"refusals":0}`)
	if n := count(t, path, `SELECT count(*) FROM grant_uses`); n != 1 {
		t.Fatalf("grant_uses rows %d", n)
	}
}

// 4. Contention: 64 goroutines, one connection each.
func TestSQLiteContention(t *testing.T) {
	const workers, reps = 64, 20
	f := loadRace(t)
	for rep := 0; rep < reps; rep++ {
		// Distinct keys racing for one single-use grant.
		path := filepath.Join(t.TempDir(), "grant.db")
		b := sqlBroker(t, f.initial, &SQLite{DB: path})
		rtWant(t, b.apply(f.approve, 0), `{"result":"grant_issued"}`)
		ops := make([]any, workers)
		for i := range ops {
			ops[i] = withKey(t, f.initial, f.approval, fmt.Sprintf("race-%d", i), "")
		}
		rtWant(t, b.apply(map[string]any{"op": "concurrent", "operations": ops}, 1),
			fmt.Sprintf(`{"outcomes":{"GRANT_EXHAUSTED":%d,"effect_completed":1},"result":"concurrent"}`, workers-1))
		for q, want := range map[string]int64{
			`SELECT count(*) FROM receipts WHERE status = 'observed_success'`: 1,
			`SELECT count(*) FROM receipt_events WHERE cause = 'reserve'`:     1,
			`SELECT count(*) FROM grant_uses`:                                 1,
			`SELECT uses FROM grants WHERE label = 'g1'`:                      1,
			`SELECT count(*) FROM audit_refusals`:                             workers - 1,
		} {
			if got := count(t, path, q); got != want {
				t.Fatalf("rep %d grant race: %s = %d, want %d", rep, q, got, want)
			}
		}

		// One idempotency key: exactly one dispatch.
		path = filepath.Join(t.TempDir(), "key.db")
		b = sqlBroker(t, f.initial, &SQLite{DB: path})
		for i := range ops {
			ops[i] = withKey(t, f.initial, f.allow, "same-key", "")
		}
		rtWant(t, b.apply(map[string]any{"op": "concurrent", "operations": ops}, 0),
			fmt.Sprintf(`{"outcomes":{"effect_completed":1,"replayed":%d},"result":"concurrent"}`, workers-1))
		for q, want := range map[string]int64{
			`SELECT count(*) FROM receipt_events WHERE cause = 'dispatch'`: 1,
			`SELECT value FROM counters WHERE name = 'next_permit'`:        1,
			`SELECT count(*) FROM adapter_effects`:                         1,
		} {
			if got := count(t, path, q); got != want {
				t.Fatalf("rep %d same key: %s = %d, want %d", rep, q, got, want)
			}
		}
	}
}

// Each concurrent worker has its own connection, opened for the group.
func TestConcurrentWorkersOwnConnections(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	main, err := (&SQLite{DB: path}).open()
	if err != nil {
		t.Fatal(err)
	}
	defer main.close()
	seen := map[*sql.Conn]bool{main.conn: true}
	for i := 0; i < 8; i++ {
		w, err := main.fork()
		if err != nil {
			t.Fatal(err)
		}
		c := w.(*sqlLedger).conn
		if seen[c] {
			t.Fatal("worker connection reused")
		}
		seen[c] = true
		defer w.close()
	}
}

// 6. sqlite-info reports the pinned engine and the settings.
func TestInfo(t *testing.T) {
	info, err := Info()
	if err != nil {
		t.Fatal(err)
	}
	s := string(sjson.Encode(info))
	for _, want := range []string{
		`"sqlite_version":"3.54.1"`,
		`"source_id":"2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"`,
		`"settings":{"busy_timeout":5000,"foreign_keys":1,"journal_mode":"wal","synchronous":2,"trusted_schema":0}`,
		`"schema_version":"1"`, `"OMIT_LOAD_EXTENSION"`, `"THREADSAFE=1"`, `"DQS=0"`, `"ENABLE_COLUMN_METADATA"`,
	} {
		if !strings.Contains(s, want) {
			t.Errorf("sqlite-info lacks %s: %s", want, s)
		}
	}
}
