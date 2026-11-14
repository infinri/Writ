package broker

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"

	"writ.dev/m1/proto/internal/sjson"
)

// LEDGER-SQLITE.md "Storage failures" (D-024), Go unit tests.

// validLedger creates a ledger file from ledger.sql through the Go prototype
// and closes it.
func validLedger(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "l.db")
	l, err := (&SQLite{DB: path}).open()
	if err != nil {
		t.Fatal(err)
	}
	l.close()
	return path
}

func execOn(t *testing.T, path, stmt string) {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(stmt); err != nil {
		t.Fatal(err)
	}
}

func dump(t *testing.T, path string) string {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.Query(`SELECT type, name, coalesce(sql, '') FROM sqlite_schema ORDER BY name`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	s := ""
	for rows.Next() {
		var a, b, c string
		if err := rows.Scan(&a, &b, &c); err != nil {
			t.Fatal(err)
		}
		s += a + "|" + b + "|" + c + "\n"
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	var v string
	if err := db.QueryRow(`SELECT group_concat(key || '=' || value) FROM meta`).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return s + v
}

func scenarioOn(t *testing.T, path string) map[string]any {
	t.Helper()
	f := loadRace(t)
	in := map[string]any{"initial": f.initial, "steps": []any{f.allow}}
	return RunWith(sjson.Clone(in), &SQLite{DB: path})
}

// Rule 1: every table of ledger.sql must exist; no step runs.
func TestOpenRefusesMissingTable(t *testing.T) {
	if len(tables) != 9 || tables[0] != "meta" || tables[8] != "adapter_effects" {
		t.Fatalf("tables parsed from ledger.sql: %v", tables)
	}
	for _, table := range tables[1:] {
		t.Run(table, func(t *testing.T) {
			path := validLedger(t)
			execOn(t, path, `DROP TABLE `+table)
			before := dump(t, path)
			rtWant(t, scenarioOn(t, path), `{"error":"LEDGER_UNAVAILABLE"}`)
			if dump(t, path) != before {
				t.Fatal("ledger modified")
			}
		})
	}
}

// Extra schema objects are not checked.
func TestOpenAcceptsExtraObjects(t *testing.T) {
	path := validLedger(t)
	execOn(t, path, `CREATE TABLE extra (x)`)
	rtWant(t, scenarioOn(t, path), `{"final":{"consumed_permits":1,"effects_applied":1,"grant_uses":{},"receipts":{"observed_success":1},"refusals":0},"steps":[{"result":"effect_completed"}]}`)
}

// Rule 1: a read-only file fails at open and is not modified.
func TestOpenRefusesReadOnlyFile(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root ignores file permissions")
	}
	path := validLedger(t)
	before := dump(t, path)
	if err := os.Chmod(path, 0o444); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(path, 0o644) })
	rtWant(t, scenarioOn(t, path), `{"error":"LEDGER_UNAVAILABLE"}`)
	if dump(t, path) != before {
		t.Fatal("ledger modified")
	}
}

// Rule 2: a failed refusal audit keeps the original refusal, is not counted,
// and the scenario continues.
func TestAuditFailureKeepsRefusal(t *testing.T) {
	f := loadRace(t)
	path := validLedger(t)
	execOn(t, path, `CREATE TRIGGER inj BEFORE INSERT ON audit_refusals BEGIN SELECT RAISE(ABORT, 'injected'); END`)
	bad := sjson.Clone(f.allow).(map[string]any)
	delete(bad, "seal")
	in := map[string]any{"initial": f.initial, "steps": []any{bad, f.allow}}
	rtWant(t, RunWith(sjson.Clone(in), &SQLite{DB: path}),
		`{"final":{"consumed_permits":1,"effects_applied":1,"grant_uses":{},"receipts":{"observed_success":1},"refusals":0},"steps":[{"code":"INGRESS_UNAUTHENTICATED","result":"refused"},{"result":"effect_completed"}]}`)
}
