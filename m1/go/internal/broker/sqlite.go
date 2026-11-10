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
	"regexp"
	"runtime/debug"
	"sync/atomic"
	"time"

	"github.com/mattn/go-sqlite3"

	"writ.dev/m1/proto/internal/sjson"
)

// Schema is m1/fixtures/broker/ledger.sql, byte for byte (checked by a test).
//
//go:embed ledger.sql
var Schema string

const driverName = "writ-sqlite3"

// connSettings are the connection-only settings (LEDGER-SQLITE.md
// "Connection settings"), applied by the connect hook right after open. They
// change nothing in the file. journal_mode=WAL (persistent) and
// synchronous=FULL follow in settle: on the first connection only after
// initialization has verified the file (S-1, D-025). The driver itself runs
// busy_timeout, locking_mode=NORMAL and synchronous=NORMAL before the hook;
// none of them is persistent.
var connSettings = []string{
	"PRAGMA busy_timeout = 5000",
	"PRAGMA foreign_keys = ON",
	"PRAGMA trusted_schema = OFF",
}

func init() {
	sql.Register(driverName, &sqlite3.SQLiteDriver{ConnectHook: func(c *sqlite3.SQLiteConn) error {
		for _, s := range connSettings {
			if _, err := c.Exec(s, nil); err != nil {
				return err
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

// writable refuses an existing file that cannot be opened for writing.
// SQLite would silently open it read-only, and a BEGIN IMMEDIATE on a
// read-only WAL database succeeds, so initialization alone would not fail.
// Opening and closing the descriptor does not modify the file.
func writable(p string) error {
	f, err := os.OpenFile(p, os.O_RDWR, 0)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	return f.Close()
}

func exists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

// createIfAbsent creates path exclusively; created is false if it already
// existed (made by an earlier run or another process).
func createIfAbsent(path string) (created bool, err error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, fs.ErrExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, f.Close()
}

var sidecarSuffixes = []string{"-journal", "-wal", "-shm"}

// removeCreated removes a database file this run created and the sidecar
// files in absent (paths that did not exist before this run opened the
// database), when they are regular files (S-2, D-025).
func removeCreated(path string, absent []string) {
	for _, p := range absent {
		if st, err := os.Lstat(p); err == nil && st.Mode().IsRegular() {
			os.Remove(p)
		}
	}
	os.Remove(path)
}

// open opens and initializes the ledger (LEDGER-SQLITE.md "Storage failures"
// rule 1, S-1 and S-2): create or open the file, connection-only settings,
// one transaction creating or verifying the schema, then journal_mode=WAL and
// synchronous=FULL. If the run created the file and initialization fails,
// the file and the sidecars it created are removed.
func (s *SQLite) open() (l *sqlLedger, err error) {
	path, created := s.DB, false
	if s.Dir != "" {
		if path, err = newFile(s.Dir); err != nil {
			return nil, tagged("open", err)
		}
		created = true
	} else if created, err = createIfAbsent(path); err != nil {
		return nil, tagged("open", err)
	}
	if created {
		var absent []string
		for _, x := range sidecarSuffixes {
			if !exists(path + x) {
				absent = append(absent, path+x)
			}
		}
		defer func() {
			if err != nil {
				removeCreated(path, absent)
			}
		}()
	} else if err := writable(path); err != nil {
		return nil, tagged("open", err)
	}
	if l, err = openSQLLedger(path); err != nil {
		return nil, tagged("open", err)
	}
	if err = l.init(); err == nil {
		err = l.settle()
	}
	if err != nil {
		l.close()
		return nil, tagged("initialize", err)
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

// settle applies the persistent and durability settings after the
// connection-only ones: journal_mode=WAL (must read back wal), then
// synchronous=FULL.
func (l *sqlLedger) settle() error {
	var mode string
	if err := l.conn.QueryRowContext(ctx, `PRAGMA journal_mode = WAL`).Scan(&mode); err != nil {
		return err
	}
	if mode != "wal" {
		return errNotWAL
	}
	_, err := l.conn.ExecContext(ctx, `PRAGMA synchronous = FULL`)
	return err
}

var errNotWAL = errors.New("journal mode is not wal")

// reopen is a later connection (restart_broker, a concurrent worker): no
// initialization; a failure is an operation failure (rule 2).
func reopen(path string) (*sqlLedger, error) {
	l, err := openSQLLedger(path)
	if err == nil {
		if err = l.settle(); err != nil {
			l.close()
		}
	}
	if err != nil {
		return nil, tagged("open", err)
	}
	return l, nil
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

// tables are the tables ledger.sql creates, in order.
var tables = func() []string {
	var t []string
	for _, m := range regexp.MustCompile(`(?m)^CREATE TABLE (\w+)`).FindAllStringSubmatch(Schema, -1) {
		t = append(t, m[1])
	}
	return t
}()

// init creates the schema in one transaction if the database is empty, and
// otherwise requires schema version 1 and every table of ledger.sql (extra
// schema objects are not checked).
func (l *sqlLedger) init() error {
	return l.tx(nil, "", func(tx *sql.Tx) error {
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
			return errSchema
		}
		for _, t := range tables {
			var n int64
			if err := tx.QueryRow(`SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = ?`, t).Scan(&n); err != nil {
				return err
			}
			if n != 1 {
				return errSchema
			}
		}
		return nil
	})
}

var (
	errRollback = errors.New("rollback")
	errSchema   = errors.New("ledger schema version or table wrong")
)

// tx runs f in one BEGIN IMMEDIATE transaction: committed if f returns nil,
// rolled back otherwise (errRollback is a rollback without error). Its
// timing is recorded in t as transaction name: begin_ns brackets BeginTx
// (which issues BEGIN IMMEDIATE, including busy waits), body_ns f, end_ns
// Commit or Rollback. A BeginTx error records begin_ns with body_ns and
// end_ns 0 and outcome rollback; a Commit error is outcome rollback. The
// D-026 error member records the first database or integrity error and
// where it was raised (begin, body, end); errRollback is not an error.
func (l *sqlLedger) tx(t *opTiming, name string, f func(*sql.Tx) error) error {
	t0 := time.Now()
	tx, err := l.conn.BeginTx(ctx, nil)
	t1 := time.Now()
	if err != nil {
		t.addTx(name, txTiming{sql: true, begin: t1.Sub(t0), err: newTxError("begin", err)})
		return txErr(name, err)
	}
	err = f(tx)
	t2 := time.Now()
	if err != nil {
		rbErr := tx.Rollback()
		tt := txTiming{sql: true, begin: t1.Sub(t0), body: t2.Sub(t1), end: time.Since(t2)}
		if err != errRollback {
			tt.err = newTxError("body", err)
		} else if rbErr != nil {
			tt.err = newTxError("end", rbErr)
		}
		t.addTx(name, tt)
		if err == errRollback {
			return nil
		}
		return txErr(name, err)
	}
	err = tx.Commit()
	tt := txTiming{sql: true, begin: t1.Sub(t0), body: t2.Sub(t1), end: time.Since(t2), commit: err == nil}
	if err != nil {
		tt.err = newTxError("end", err)
	}
	t.addTx(name, tt)
	return txErr(name, err)
}

// txErr tags a failure of a named logical transaction ("" is initialization
// or final, tagged by the caller).
func txErr(name string, err error) error {
	if name == "" {
		return err
	}
	return tagged("transaction "+name, err)
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

func (l *sqlLedger) reserve(t *opTiming, r reservation) (outcome string, err error) {
	err = l.tx(t, "reserve", func(tx *sql.Tx) error {
		var fp, status string
		var label sql.NullString
		e := tx.QueryRow(`SELECT fingerprint, grant_label, status FROM receipts WHERE tenant_id = ? AND idempotency_key = ?`,
			r.tenant, r.key).Scan(&fp, &label, &status)
		if e == nil {
			if outcome = r.existingOutcome(fp, label.String, status); outcome != "" {
				return errRollback
			}
			return nil // a reservation resumes: commit nothing
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
	if !bindingValid(g.binding) {
		return nil, errGrantIntegrity
	}
	g.expires = time.Unix(s, ns).UTC()
	return &g, nil
}

var errGrantIntegrity = errors.New("stored grant binding fails integrity")

// bindingStrings are the string members of a stored grant binding; the only
// other member is requested_capabilities (array of strings).
var bindingStrings = []string{"action_digest", "action_kind", "approval_request_id", "canonical_resource_id",
	"policy_bundle_sha", "principal_id", "tenant_id", "verdict"}

// bindingValid is stored-grant integrity (LEDGER-SQLITE.md "Storage
// failures" rule 5, C-4): strict JSON, exactly its own D-006 canonical text,
// and an object with exactly the binding members and their types.
func bindingValid(text string) bool {
	v, perr := sjson.Parse(text)
	if perr != nil {
		return false
	}
	m, ok := v.(map[string]any)
	if !ok || len(m) != len(bindingStrings)+1 {
		return false
	}
	for _, k := range bindingStrings {
		if _, ok := m[k].(string); !ok {
			return false
		}
	}
	caps, ok := m["requested_capabilities"].([]any)
	if !ok {
		return false
	}
	for _, c := range caps {
		if _, ok := c.(string); !ok {
			return false
		}
	}
	c, err := sjson.Canonical(v)
	return err == nil && string(c) == text
}

func (l *sqlLedger) dispatch(t *opTiming, tenant, key string) (seq int64, err error) {
	err = l.tx(t, "dispatch", func(tx *sql.Tx) error {
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

func (l *sqlLedger) adapter(t *opTiming, check func() string, permitID string, effect bool) (code string, err error) {
	err = l.tx(t, "adapter", func(tx *sql.Tx) error {
		if code = check(); code != "" {
			return errRollback
		}
		var x int64
		e := tx.QueryRow(`SELECT 1 FROM adapter_consumed_permits WHERE permit_id = ?`, permitID).Scan(&x)
		if e == nil {
			code = "PERMIT_REPLAYED"
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
	if err != nil {
		code = ""
	}
	return code, err
}

func (l *sqlLedger) observe(t *opTiming, tenant, key, status string) error {
	return l.tx(t, "observe", func(tx *sql.Tx) error {
		res, e := tx.Exec(`UPDATE receipts SET status = ? WHERE tenant_id = ? AND idempotency_key = ? AND status = 'dispatched'`, status, tenant, key)
		if e := one(res, e, "observe"); e != nil {
			return e
		}
		return event(tx, tenant, key, status, "observe")
	})
}

func (l *sqlLedger) addGrant(t *opTiming, label string, g grant) (inUse bool, err error) {
	err = l.tx(t, "approve", func(tx *sql.Tx) error {
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

func (l *sqlLedger) reconcile(t *opTiming) (n int64, err error) {
	err = l.tx(t, "reconcile", func(tx *sql.Tx) error {
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

func (l *sqlLedger) audit(t *opTiming, code string) error {
	return l.tx(t, "audit", func(tx *sql.Tx) error {
		_, err := tx.Exec(`INSERT INTO audit_refusals (code) VALUES (?)`, code)
		return err
	})
}

func (l *sqlLedger) final() (f map[string]any, err error) {
	defer func() { err = tagged("final", err) }()
	err = l.tx(nil, "", func(tx *sql.Tx) error {
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
	w, err := reopen(l.path)
	if err != nil {
		return nil, err
	}
	return w, nil
}

// restart closes the connection and opens a new one to the same file.
func (l *sqlLedger) restart() error {
	l.close()
	n, err := reopen(l.path)
	if err != nil {
		return err
	}
	l.db, l.conn = n.db, n.conn
	return nil
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
		return nil, tagged("open", err)
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
			return nil, tagged("final", err)
		}
	}
	rows, err := l.conn.QueryContext(ctx, `PRAGMA compile_options`)
	if err != nil {
		return nil, tagged("final", err)
	}
	defer rows.Close()
	options := []any{}
	for rows.Next() {
		var o string
		if err := rows.Scan(&o); err != nil {
			return nil, tagged("final", err)
		}
		options = append(options, o)
	}
	if err := rows.Err(); err != nil {
		return nil, tagged("final", err)
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
