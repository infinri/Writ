package broker

import (
	"context"
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime/debug"
	"sync/atomic"
	"time"

	"github.com/mattn/go-sqlite3"
)

// Schema is m1/fixtures/broker/ledger.sql, byte for byte (checked by a test).
//
//go:embed ledger.sql
var Schema string

const driverName = "writ-sqlite3"

// settings are applied to every connection right after open, in this order
// (LEDGER-SQLITE.md "Connection settings").
var settings = []string{
	"PRAGMA journal_mode = WAL",
	"PRAGMA synchronous = FULL",
	"PRAGMA busy_timeout = 5000",
	"PRAGMA foreign_keys = ON",
	"PRAGMA trusted_schema = OFF",
}

func init() {
	sql.Register(driverName, &sqlite3.SQLiteDriver{ConnectHook: func(c *sqlite3.SQLiteConn) error {
		for _, s := range settings {
			if _, err := c.Exec(s, nil); err != nil {
				return fmt.Errorf("%s: %w", s, err)
			}
		}
		return nil
	}})
}

// SQLite selects the SQLite ledger: a new database file in Dir for every
// scenario, or the one database DB for the whole process (created if absent,
// never reset).
type SQLite struct {
	Dir string
	DB  string
}

var fileSeq atomic.Int64

// newFile creates an empty database file in dir that no earlier run used.
func newFile(dir string) (string, error) {
	for {
		p := filepath.Join(dir, fmt.Sprintf("%d-%d.db", os.Getpid(), fileSeq.Add(1)))
		if exists(p+"-wal") || exists(p+"-shm") {
			continue
		}
		f, err := os.OpenFile(p, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o600)
		if errors.Is(err, fs.ErrExist) {
			continue
		}
		if err != nil {
			return "", err
		}
		return p, f.Close()
	}
}

func exists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

func (s *SQLite) open() (*sqlLedger, error) {
	path := s.DB
	if s.Dir != "" {
		var err error
		if path, err = newFile(s.Dir); err != nil {
			return nil, err
		}
	}
	l, err := openSQLLedger(path)
	if err != nil {
		return nil, err
	}
	if err := l.init(); err != nil {
		l.close()
		return nil, err
	}
	return l, nil
}

// sqlLedger is one dedicated connection to a ledger database file.
type sqlLedger struct {
	path string
	db   *sql.DB
	conn *sql.Conn
}

var ctx = context.Background()

func openSQLLedger(path string) (*sqlLedger, error) {
	l := &sqlLedger{path: path}
	return l, l.connect()
}

// connect opens a new connection; _txlock=immediate makes every BeginTx a
// BEGIN IMMEDIATE.
func (l *sqlLedger) connect() error {
	db, err := sql.Open(driverName, l.path+"?_txlock=immediate")
	if err != nil {
		return err
	}
	db.SetMaxOpenConns(1)
	conn, err := db.Conn(ctx)
	if err != nil {
		db.Close()
		return err
	}
	l.db, l.conn = db, conn
	return nil
}

// init creates the schema in one transaction if the database is empty, and
// otherwise requires schema version 1.
func (l *sqlLedger) init() error {
	return l.tx(func(tx *sql.Tx) error {
		var n int64
		if err := tx.QueryRow(`SELECT count(*) FROM sqlite_schema`).Scan(&n); err != nil {
			return err
		}
		if n == 0 {
			_, err := tx.Exec(Schema)
			return err
		}
		var v string
		if err := tx.QueryRow(`SELECT value FROM meta WHERE key = 'schema_version'`).Scan(&v); err != nil {
			return err
		}
		if v != "1" {
			return fmt.Errorf("unsupported ledger schema version %q", v)
		}
		return nil
	})
}

var errRollback = errors.New("rollback")

// tx runs f in one BEGIN IMMEDIATE transaction: committed if f returns nil,
// rolled back otherwise (errRollback is a rollback without error).
func (l *sqlLedger) tx(f func(*sql.Tx) error) error {
	tx, err := l.conn.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := f(tx); err != nil {
		tx.Rollback()
		if err == errRollback {
			return nil
		}
		return err
	}
	return tx.Commit()
}

func one(res sql.Result, err error, what string) error {
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("%s: %d rows changed, want 1", what, n)
	}
	return nil
}

func (l *sqlLedger) serialize() func() { return func() {} }

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (l *sqlLedger) reserve(r reservation) (outcome string, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		var fp, status string
		var label sql.NullString
		e := tx.QueryRow(`SELECT fingerprint, grant_label, status FROM receipts WHERE tenant_id = ? AND idempotency_key = ?`,
			r.tenant, r.key).Scan(&fp, &label, &status)
		if e == nil {
			outcome = r.existingOutcome(fp, label.String, status)
			return errRollback
		}
		if e != sql.ErrNoRows {
			return e
		}
		if r.crashBefore {
			outcome = outcomeCrashed
			return errRollback
		}
		if r.approval {
			g, e := grantRow(tx, r.grantLabel)
			if e != nil {
				return e
			}
			if outcome = r.check(g); outcome != "" {
				return errRollback
			}
			res, e := tx.Exec(`UPDATE grants SET uses = uses + 1 WHERE label = ? AND uses < max_uses`, r.grantLabel)
			if e != nil {
				return e
			}
			if n, e := res.RowsAffected(); e != nil || n != 1 {
				if e != nil {
					return e
				}
				outcome = "GRANT_EXHAUSTED"
				return errRollback
			}
			if _, e := tx.Exec(`INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES (?, ?, ?)`,
				r.grantLabel, r.tenant, r.key); e != nil {
				return e
			}
		}
		if _, e := tx.Exec(`INSERT INTO receipts (tenant_id, idempotency_key, fingerprint, grant_label, status) VALUES (?, ?, ?, ?, 'reserved')`,
			r.tenant, r.key, r.fingerprint, nullable(r.grantLabel)); e != nil {
			return e
		}
		return event(tx, r.tenant, r.key, "reserved", "reserve")
	})
	return outcome, err
}

func event(tx *sql.Tx, tenant, key, status, cause string) error {
	_, err := tx.Exec(`INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES (?, ?, ?, ?)`,
		tenant, key, status, cause)
	return err
}

func grantRow(tx *sql.Tx, label string) (*grant, error) {
	var g grant
	var s, ns int64
	err := tx.QueryRow(`SELECT binding, expires_s, expires_ns, max_uses, uses FROM grants WHERE label = ?`, label).
		Scan(&g.binding, &s, &ns, &g.maxUses, &g.uses)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	g.expires = time.Unix(s, ns).UTC()
	return &g, nil
}

func (l *sqlLedger) dispatch(tenant, key string) (seq int64, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		if _, e := tx.Exec(`UPDATE counters SET value = value + 1 WHERE name = 'next_permit'`); e != nil {
			return e
		}
		if e := tx.QueryRow(`SELECT value FROM counters WHERE name = 'next_permit'`).Scan(&seq); e != nil {
			return e
		}
		res, e := tx.Exec(`UPDATE receipts SET status = 'dispatched' WHERE tenant_id = ? AND idempotency_key = ? AND status = 'reserved'`, tenant, key)
		if e := one(res, e, "dispatch"); e != nil {
			return e
		}
		return event(tx, tenant, key, "dispatched", "dispatch")
	})
	return seq, err
}

func (l *sqlLedger) consume(permitID string, effect bool) (replayed bool, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		var x int64
		e := tx.QueryRow(`SELECT 1 FROM adapter_consumed_permits WHERE permit_id = ?`, permitID).Scan(&x)
		if e == nil {
			replayed = true
			return errRollback
		}
		if e != sql.ErrNoRows {
			return e
		}
		if _, e := tx.Exec(`INSERT INTO adapter_consumed_permits (permit_id) VALUES (?)`, permitID); e != nil {
			return e
		}
		if effect {
			if _, e := tx.Exec(`INSERT INTO adapter_effects (permit_id) VALUES (?)`, permitID); e != nil {
				return e
			}
		}
		return nil
	})
	return replayed, err
}

func (l *sqlLedger) observe(tenant, key, status string) error {
	return l.tx(func(tx *sql.Tx) error {
		res, e := tx.Exec(`UPDATE receipts SET status = ? WHERE tenant_id = ? AND idempotency_key = ? AND status = 'dispatched'`, status, tenant, key)
		if e := one(res, e, "observe"); e != nil {
			return e
		}
		return event(tx, tenant, key, status, "observe")
	})
}

func (l *sqlLedger) addGrant(label string, g grant) (inUse bool, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		var x int64
		e := tx.QueryRow(`SELECT 1 FROM grants WHERE label = ?`, label).Scan(&x)
		if e == nil {
			inUse = true
			return errRollback
		}
		if e != sql.ErrNoRows {
			return e
		}
		_, e = tx.Exec(`INSERT INTO grants (label, binding, expires_s, expires_ns, max_uses, uses) VALUES (?, ?, ?, ?, ?, 0)`,
			label, g.binding, g.expires.Unix(), int64(g.expires.Nanosecond()), g.maxUses)
		return e
	})
	return inUse, err
}

func (l *sqlLedger) reconcile() (n int64, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		if _, e := tx.Exec(`INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause)
			SELECT tenant_id, idempotency_key, 'unknown_reconcile', 'reconcile' FROM receipts WHERE status = 'dispatched'
			ORDER BY tenant_id, idempotency_key`); e != nil {
			return e
		}
		res, e := tx.Exec(`UPDATE receipts SET status = 'unknown_reconcile' WHERE status = 'dispatched'`)
		if e != nil {
			return e
		}
		n, e = res.RowsAffected()
		return e
	})
	return n, err
}

func (l *sqlLedger) audit(code string) error {
	return l.tx(func(tx *sql.Tx) error {
		_, err := tx.Exec(`INSERT INTO audit_refusals (code) VALUES (?)`, code)
		return err
	})
}

func (l *sqlLedger) final() (f map[string]any, err error) {
	err = l.tx(func(tx *sql.Tx) error {
		count := func(table string) (int64, error) {
			var n int64
			return n, tx.QueryRow(`SELECT count(*) FROM ` + table).Scan(&n)
		}
		effects, e := count("adapter_effects")
		if e != nil {
			return e
		}
		consumed, e := count("adapter_consumed_permits")
		if e != nil {
			return e
		}
		refusals, e := count("audit_refusals")
		if e != nil {
			return e
		}
		uses, e := pairs(tx, `SELECT label, uses FROM grants`)
		if e != nil {
			return e
		}
		receipts, e := pairs(tx, `SELECT status, count(*) FROM receipts GROUP BY status`)
		if e != nil {
			return e
		}
		f = map[string]any{
			"effects_applied":  effects,
			"grant_uses":       uses,
			"receipts":         receipts,
			"consumed_permits": consumed,
			"refusals":         refusals,
		}
		return errRollback
	})
	return f, err
}

func pairs(tx *sql.Tx, query string) (map[string]any, error) {
	rows, err := tx.Query(query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	m := map[string]any{}
	for rows.Next() {
		var k string
		var n int64
		if err := rows.Scan(&k, &n); err != nil {
			return nil, err
		}
		m[k] = n
	}
	return m, rows.Err()
}

func (l *sqlLedger) fork() (ledger, error) {
	w, err := openSQLLedger(l.path)
	if err != nil {
		return nil, err
	}
	return w, nil
}

// restart closes the connection and opens a new one to the same file.
func (l *sqlLedger) restart() error {
	l.close()
	return l.connect()
}

func (l *sqlLedger) close() {
	if l.conn != nil {
		l.conn.Close()
	}
	if l.db != nil {
		l.db.Close()
	}
	l.db, l.conn = nil, nil
}

// Info is the `proto sqlite-info` diagnostic, read back from a fresh
// connection to a new temporary database.
func Info() (map[string]any, error) {
	dir, err := os.MkdirTemp("", "writ-sqlite-info-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)
	l, err := (&SQLite{Dir: dir}).open()
	if err != nil {
		return nil, err
	}
	defer l.close()
	var version, sourceID, journal, schemaVersion string
	var synchronous, busy, fk, trusted int64
	for _, q := range []struct {
		query string
		dest  any
	}{
		{`SELECT sqlite_version()`, &version},
		{`SELECT sqlite_source_id()`, &sourceID},
		{`PRAGMA journal_mode`, &journal},
		{`PRAGMA synchronous`, &synchronous},
		{`PRAGMA busy_timeout`, &busy},
		{`PRAGMA foreign_keys`, &fk},
		{`PRAGMA trusted_schema`, &trusted},
		{`SELECT value FROM meta WHERE key = 'schema_version'`, &schemaVersion},
	} {
		if err := l.conn.QueryRowContext(ctx, q.query).Scan(q.dest); err != nil {
			return nil, fmt.Errorf("%s: %w", q.query, err)
		}
	}
	rows, err := l.conn.QueryContext(ctx, `PRAGMA compile_options`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	options := []any{}
	for rows.Next() {
		var o string
		if err := rows.Scan(&o); err != nil {
			return nil, err
		}
		options = append(options, o)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return map[string]any{
		"sqlite_version":  version,
		"source_id":       sourceID,
		"compile_options": options,
		"settings": map[string]any{
			"journal_mode":   journal,
			"synchronous":    synchronous,
			"busy_timeout":   busy,
			"foreign_keys":   fk,
			"trusted_schema": trusted,
		},
		"schema_version": schemaVersion,
		"driver":         driverVersion(),
	}, nil
}

func driverVersion() string {
	const path = "github.com/mattn/go-sqlite3"
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, d := range bi.Deps {
			if d.Path == path {
				return path + " " + d.Version
			}
		}
	}
	return path + " (version unknown)"
}
