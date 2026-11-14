package broker

import (
	"errors"
	"fmt"
	"os"

	"github.com/mattn/go-sqlite3"
)

// Ledger diagnostics (LEDGER-SQLITE.md "Storage failures" rule 4, D-025):
// stderr carries only a fixed category (open, initialize, transaction
// <name>, final, worker) and the SQLite primary result code name,
// STATE_CONFLICT (D-029), or io for any other failure. Never a path, a file
// name, error text, a credential, a key or a seal.

// ledgerErr is a ledger failure tagged with its diagnostic category.
type ledgerErr struct {
	category string
	err      error
}

func (e *ledgerErr) Error() string { return e.category + " " + codeName(e.err) }
func (e *ledgerErr) Unwrap() error { return e.err }

// tagged gives err its category unless it already has one.
func tagged(category string, err error) error {
	var le *ledgerErr
	if err == nil || errors.As(err, &le) {
		return err
	}
	return &ledgerErr{category, err}
}

// Diagnostic is the sanitized operator text for a ledger failure; an
// untagged failure is reported in the open category.
func Diagnostic(err error) string {
	return tagged("open", err).Error()
}

func diagnose(err error) {
	fmt.Fprintln(os.Stderr, "ledger error:", Diagnostic(err))
}

var primaryCodes = map[sqlite3.ErrNo]string{
	1: "SQLITE_ERROR", 2: "SQLITE_INTERNAL", 3: "SQLITE_PERM", 4: "SQLITE_ABORT", 5: "SQLITE_BUSY",
	6: "SQLITE_LOCKED", 7: "SQLITE_NOMEM", 8: "SQLITE_READONLY", 9: "SQLITE_INTERRUPT", 10: "SQLITE_IOERR",
	11: "SQLITE_CORRUPT", 12: "SQLITE_NOTFOUND", 13: "SQLITE_FULL", 14: "SQLITE_CANTOPEN", 15: "SQLITE_PROTOCOL",
	16: "SQLITE_EMPTY", 17: "SQLITE_SCHEMA", 18: "SQLITE_TOOBIG", 19: "SQLITE_CONSTRAINT", 20: "SQLITE_MISMATCH",
	21: "SQLITE_MISUSE", 22: "SQLITE_NOLFS", 23: "SQLITE_AUTH", 24: "SQLITE_FORMAT", 25: "SQLITE_RANGE",
	26: "SQLITE_NOTADB", 27: "SQLITE_NOTICE", 28: "SQLITE_WARNING", 100: "SQLITE_ROW", 101: "SQLITE_DONE",
}

// codeName is the SQLite primary result code name of err, STATE_CONFLICT
// for an application state conflict (D-029), or io.
func codeName(err error) string {
	if errors.Is(err, errStateConflict) {
		return "STATE_CONFLICT"
	}
	var se sqlite3.Error
	if errors.As(err, &se) {
		if n, ok := primaryCodes[se.Code]; ok {
			return n
		}
	}
	return "io"
}
